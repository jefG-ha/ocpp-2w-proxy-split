# websocket proxy
# To later be expanded with OCPP capabilities in order to handle local smart charging.

import asyncio
import logging
import time

import websockets
import websockets.asyncio
import websockets.asyncio.server
from websockets.exceptions import ConnectionClosed, ConnectionClosedError
from websockets.frames import CloseCode

import ssl
from pathlib import Path
import argparse
import configparser

__version__ = "0.1.0"

config = configparser.ConfigParser()

logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s', level=logging.DEBUG, datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger("OCPP2WProxy")

class OCPP2WProxy: # Forward declaration
    pass

# main class
class OCPP2WProxy:
    # Static dict of OCPP2WProxy instances. key is charger_id
    proxy_list: dict[OCPP2WProxy] = {}

    def __init__(self, websocket: websockets.server.WebSocketServerProtocol, charger_id: str):
        # Store the websocket for later     
        logger.debug(websocket.request)
        self.ws = websocket
        self.charger_id = charger_id

        # Chech that charger id looks reasonable
        if not charger_id.isalnum():
            logger.error(f"Charger ID '{charger_id}' is not alphanumeric")
            raise Exception("Charger ID is not alphanumeric")

        # Initialize table of CSMS call ids sent to the charger in order to respond back 
        # (only) to the CSMS that issued the call
        self.call_ids: dict[str, str] = {}

        # Insert new OCPP2WProxy instance in the (static) dict of instances.
        self.proxy_list[charger_id] = self

    async def close(self):
        """Close all connections to the charger and primary, secondary server"""
        try:
            await self.ws.close()
            await self.primary_connection.close()
            if self.secondary_connection:
                await self.secondary_connection.close()
        except Exception as e:
            pass # Ignore exceptions

    async def check_delete_old(charger_id: str):
        """Check if there are any old instances of this charger in the proxy list"""
        if charger_id in OCPP2WProxy.proxy_list:
            logger.info(f"Charger ID {self.charger_id} already exists. Closing and deleting")
            proxy = OCPP2WProxy.proxy_list[charger_id]
            await proxy.close()
            del OCPP2WProxy.proxy_list[charger_id]

    async def run(self):
        """Main loop for this proxy. This is where all the magic happens."""

        # Create connections to the two CSMSes.
        # Forward any available Authorization and User-Agent headers
        headers = {}
        if "Authorization" in self.ws.request.headers:
            headers["Authorization"] = self.ws.request.headers["Authorization"]
            logger.debug(f'Authorization header set to {headers["Authorization"]}')
        user_agent = self.ws.request.headers.get("User-Agent", None) 
        subprotocols = self.ws.request.headers.get("Sec-WebSocket-Protocol", ["ocpp1.6"])
        primary_url = config.primary_server + "/" + self.charger_id
        if config.secondary_server:
            secondary_url = config.secondary_server + "/" + self.charger_id

        try:
            self.primary_connection = await websockets.connect(
                uri=primary_url,
                user_agent_header=user_agent,
                additional_headers=headers,
                subprotocols=subprotocols,
            )
            logger.info(f"Connected to primary server @ {primary_url}")

            # Connect to secondary server if it is enabled.
            if config.secondary_server:
                self.secondary_connection = await websockets.connect(
                    uri=secondary_url,
                    user_agent_header=user_agent,
                    additional_headers=headers,
                    subprotocols=subprotocols,
                )
                logger.info(f"{self.charger_id} Connected to secondary server @ {secondary_url}")
            else:
                self.secondary_connection = None
                logger.info(f"{self.charger_id} Secondary server not enabled")
            
            # Create tasks to handle the charger. Each task each to handle receiving messages from
            # the charger, and the (one or two) CSMSes, and finally a watch dog task to take down
            # connections if connection goes stale.
            self.tasks = []
            self.tasks.append(asyncio.create_task(self.receive_charger_messages()))
            self.tasks.append(asyncio.create_task(self.receive_primary_messages()))
            if self.secondary_connection is not None:
                self.tasks.append(asyncio.create_task(self.receive_secondary_messages()))
            self.tasks.append(asyncio.create_task(self.watchdog()))

            # Wait for tasks to complete
            done, pending = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
            logger.debug(f"{self.charger_id} Task(s) completed: {done}, {pending}")

            for task in done:
                e = task.exception()
                if e:
                    logger.warning(f"{self.charger_id} (Not serious - likely connection loss) Task {task} raised exception {e} related to charger ")

            # Cancel any remaining tasks
            for task in pending:
                task.cancel()

        except websockets.exceptions.InvalidURI:
            logging.error(f"{self.charger_id} Invalid URI")
        except websockets.exceptions.ConnectionClosedError as e:
            logging.error(f"{self.charger_id} Connection closed unexpectedly: {e}")
        except websockets.exceptions.InvalidHandshake:
            logging.error(f"{self.charger_id} Handshake with the external server failed")
        except Exception as e:
            logging.error(f"{self.charger_id} Unexpected error: {e}")
        finally:
            # Always close stuff. close is well tempered, so can close even if not stablished
            await self.close()

    async def receive_charger_messages(self):
        try:
            while True:
                # Wait for a message from the charger
                message = await self.charger_connection.receive()
                # Process the received message
                # TODO - more logic. type of message, etc
                logging.info(f"{self.charger_id} ^ : {message}")
                await self.primary_connection.send(message)
                if self.secondary_connection:
                    await self.secondary_connection.send(message)

        except ConnectionClosed:
            pass

    async def receive_primary_messages(self):
        try:
            while True:
                # Wait for a message from the primary server
                # TODO: more logic
                message = await self.primary_connection.receive()
                logging.info(f"{self.charger_id} v (prim) : {message}")
                # Send it to the charger
                # TODO: more logic, like type of message, etc

                # Process the received message
                await self.charger_connection.send(message)

        except ConnectionClosed:
            pass

    async def receive_secondary_messages(self):
        try:
            while True:
                # Wait for a message from the secondary server
                message = await self.secondary_connection.receive()
                logging.info(f"{self.charger_id} v (sec) : {message}")
                # Process the received message
                # TODO
                await self.charger_connection.send(message)

        except ConnectionClosed:
            pass

    async def watchdog(self):
        """Watch time vs. timestamp updated by receiving messages from charger."""
        while True:
            # And ... sleep
            await asyncio.sleep(config.getint("host", "watchdog_interval", 30))

            elapsed = time.time() - self._last_charger_update
            if elapsed > config.getint("host", "watchdog_stale", 300):
                logger.error(f"{self.charger_id} Watch dog no for {elapsed} seconds. Closing connections")
                return

# Connection handler (charger connects)
async def on_connect(websocket: websockets.asyncio.server.ServerConnection):
    logger.debug('Connection request', websocket.request)
    # Determine charger_id (final part of path)
    path = websocket.request.path
    charger_id = path.strip("/")
    logger.info(f'{charger_id} connection request')

    try:
        # Delete any existing charger setup
        await OCPP2WProxy.delete_charger_setup(charger_id)

        # Setup
        proxy = OCPP2WProxy(websocket=websocket, charger_id=charger_id)

        # Connect and run proxy operations
        await proxy.run()

    except Exception as e:
        logger.error(f'{charger_id} Error creating OCPP2WProxy: {e}')
    finally:
        logger.info("f{charger_id} closed/done")


# Main. Decode arguments, setup handler
async def main():
    parser = argparse.ArgumentParser(
        description='ocpp-2w-proxy: A two way OCPP proxy')
    parser.add_argument('--version', action='version',
                        version=f'%(prog)s {__version__}')
    parser.add_argument(
        "--config",
        type=str,
        default="ocpp2-2w-proxy.ini",
        help="Configuration file (INI format). Default ocpp-2w-proxy.ini",
    )
    args = parser.parse_args()

    # Read config. config object is then available (via config import) to all.
    logger.warning(f"Reading config from {args.config}")
    config.read(args.config)

    host = args.host
    tls_host = args.tls_host
    if not tls_host:
        tls_host = host

    port = args.port
    tls_port = args.tls_port

    cert_chain = args.cert_chain

    tls_server = None
    if cert_chain:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(cert_chain)
        server = await websockets.serve(
            on_connect, tls_host, tls_port,subprotocols=["ocpp1.6", "ocpp2.0.1"], ssl=ssl_context
        )
    else:
        server = await websockets.serve(
            on_connect, host, port, subprotocols=["ocpp1.6", "ocpp2.0.1"]
        )

    logging.info("Proxy ready. Waiting for new connections...")
    await server.wait_closed()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        exit(0)

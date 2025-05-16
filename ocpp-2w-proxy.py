# websocket proxy
# To later be expanded with OCPP capabilities in order to handle local smart charging.

import asyncio
import logging
import signal

import websockets.asyncio
import websockets.asyncio.server
import websockets.asyncio.client
import http
import websockets
import ssl
from pathlib import Path
import argparse

"""
from ocpp.v16.enums import (
    AuthorizationStatus,
    ChargingProfileKindType,
    ChargingProfilePurposeType,
    ChargingRateUnitType,
    CiStringType,
    HashAlgorithm,
    Location,
    Measurand,
    Phase,
    ReadingContext,
    RecurrencyKind,
    UnitOfMeasure,
    ValueFormat,
)
from ocpp.v16.datatypes import (
    ChargingProfile
)
from ocpp.v16.call import (
    SetChargingProfile
) 
"""

__version__ = "0.1.0"

iso15118_certs = None

logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s', level=logging.DEBUG, datefmt='%Y-%m-%d %H:%M:%S')

global url

# Track active connections by client path
active_client_connections = {}

# Forward traffic from server (portal).
async def forward_from_server(client_ws, server_ws, path):
    try:
        async for message in server_ws:
            logging.info("v[" + path + "] " + message)
            await client_ws.send(message)
    except websockets.ConnectionClosed:
        pass

# Forward traffic from client (charger).
async def forward_from_client(client_ws, server_ws, path):
    try:
        async for message in client_ws:
            logging.info("^[" + path + "] " + message)
            await server_ws.send(message)
    except websockets.ConnectionClosed:
        pass

# Connection handler (charger connects)
async def handler(client_ws):
    logging.debug('Connection request')
    logging.debug(client_ws.request)
    path = client_ws.request.path
    charge_point_id = path.strip("/")
    logging.info(f'Connection from {charge_point_id}')

    # Close any existing connection with the same client_path
    if path in active_client_connections:
        old_ws = active_client_connections(path)
        logging.error(f'Found previous client connection for {path}, closing old.')
        await old_ws.close(reason="New connection established")
    active_client_connections[path] = client_ws

    # Establish ws/wss connection to URL.
    portal_uri = url + "/" + charge_point_id
    #requested_protocols = websocket.request.headers["Sec-WebSocket-Protocol"].split(",")  # This seems to not work. Keeping for history
    requested_protocols = ['ocpp1.6']   
    logging.info(f'Establishing connection to upstream URL @ {portal_uri}. Protocol is {requested_protocols}')

    try:
        async with websockets.connect(uri = portal_uri, subprotocols=requested_protocols, ssl = True) as server_ws:
            # Two tasks: forward data both ways
            forward_server_to_client = await asyncio.create_task(forward_from_server(client_ws, server_ws, path))
            forward_client_to_server = await asyncio.create_task(forward_from_client(client_ws, server_ws, path))

            # Wait for both tasks to complete
            done, pending = await asyncio.wait(
                [forward_client_to_server, forward_server_to_client],
                return_when=asyncio.FIRST_COMPLETED
            )

            # Cancel any remaining tasks
            for task in pending:
                task.cancel()

    except websockets.exceptions.InvalidURI:
        logging.error(f"Invalid URI: {portal_uri}")
        await client_ws.close(reason="Invalid server URI")
    except websockets.exceptions.ConnectionClosedError as e:
        logging.error(f"Connection closed unexpectedly: {e}")
        await client_ws.close(reason="External server connection closed")
    except websockets.exceptions.InvalidHandshake:
        logging.error("Handshake with the external server failed")
        await client_ws.close(reason="Failed handshake with external server")
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        await client_ws.close(reason="Unexpected server error")      
    finally:
        logging.info(f'Closing connection for {path}')
        active_client_connections.pop(path, None)
        await client_ws.close(reason="Proxy shutting down")  


# Main. Decode arguments, setup handler
async def main():
    parser = argparse.ArgumentParser(
        description='A simple websocket proxy')
    parser.add_argument('--version', action='version',
                        version=f'%(prog)s {__version__}')

    parser.add_argument('--host', type=str, default="0.0.0.0",
                        help='Host to listen on (default: 0.0.0.0)')

    parser.add_argument('--port', type=int, default=9999,
                        help='Plaintext port to listen on (default: 9999)')

    parser.add_argument('--tls-host', type=str, default=None,
                        help='TLS Host to listen on (default: value of --host (0.0.0.0))')

    parser.add_argument('--tls-port', type=int, default=9901,
                        help='TLS port to listen on (default: 9901)')

    parser.add_argument('--cert-chain', type=str, default=None,
                        help='Certificate chain to be used with TLS websockets. If not provided TLS will be disabled')

    parser.add_argument('--certs', type=str, default="/var/",
                        help='Directory containing certificates (default: ../everest-core/build/dist/etc/everest/certs)')

    parser.add_argument('certificates', type=str, default=None, nargs='?',
                        help='Directory containing certificates (default: identical to --certs')

    parser.add_argument('--url', type=str, default="ws://localhost:9000",
                        help='Websocket URL to connect to, ws:// or wss://')

    args = parser.parse_args()

    global url
    url = args.url
    if not url:
        logging.error("No --url argument supplied. Exiting.")
        return

    host = args.host
    tls_host = args.tls_host
    if not tls_host:
        tls_host = host

    port = args.port
    tls_port = args.tls_port

    cert_chain = args.cert_chain

    certs = Path(args.certs)
    if not certs.exists():
        logging.warning(
            'Directory containing certificates does not exist, ISO15118 features are not available')
    else:
        global iso15118_certs
        iso15118_certs = certs

    tls_server = None
    if cert_chain:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(cert_chain)
        server = await websockets.serve(
            handler, tls_host, tls_port,subprotocols=["ocpp1.6", "ocpp2.0.1"], ssl=ssl_context
        )
    else:
        server = await websockets.serve(
            handler, host, port, subprotocols=["ocpp1.6", "ocpp2.0.1"]
        )

    # Function to shut down the server gracefully
    def shutdown():
        print("Shutting down server...")
        server.close()
        asyncio.create_task(server.wait_closed())
        loop.stop()

    # Register signal handlers for SIGINT (Ctrl+C) and SIGTERM (Docker stop)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)

    logging.info("Proxy ready. Waiting for new connections...")
    await server.wait_closed()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        exit(0)

# ocpp-2w-proxy

A 2 way OCPP proxy with split functionality based on the RFID tag.

ocpp-2w-proxy allows chargers (one or more) to establish connections not just with one central management system (server), but to two.
This is useful for example if a setup requires that the charger wants to connect to 2 official servers (e.g. for split billing purposes), 
e.g. Employers requiring a specific OCPP server to get reimbursed for charging sessions for a company car based on a personal RFID charging bagde/token handed out.

By defining the primary and secondary RFID, this project can be used to split the traffic based on the authentication token (RFID) to the primary or secondary OCPP server defined.

The proxy works be defining a primary and a secondary RFID token and server. This governs the way in which the proxy will forward messages upstream
and downstream.

The rules are as follows:
1. All Calls (OCPP type 2) from the charger is forwarded to the primary or the secondary server based on the RFID.
2. All Replies (OCPP type 3) or Errors (OCPP type 4) received from the primary server are forwarded to the charger.
3. All Replies (OCPP type 3) or Errors (OCPP type 4) received from the secondary server are ignored and not forwarded to the charger.
4. All Calls (OCPP type 2) received from either the primary or secondary server is forward to the charger. The message_id is noted against the server.
5. All Replies (OCPP type 3) or Errors (OCPP type 4) received from the charger is forwarded to either the primary OR the secondary server depending on which one sent it (based on message_id noted in step 4).

The proxy will also keep a watch dog of stale connections. If a connection is not seen for more than `watchdog_stale` seconds, the connection will be closed and removed from the list.

## Usage

Review and update configuration in `ocpp-2w-proxy.ini`. Start the proxy with:

`python ocpp_2w_proxy.py`

## Docker

`Dockerfile` and `compose.yaml` files are included for completeness.

 
"""
websocket - WebSocket client library for Python

Copyright (C) 2010 Hiroki Ohtani(liris)

    This library is free software; you can redistribute it and/or
    modify it under the terms of the GNU Lesser General Public
    License as published by the Free Software Foundation; either
    version 2.1 of the License, or (at your option) any later version.

    This library is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
    Lesser General Public License for more details.

    You should have received a copy of the GNU Lesser General Public
    License along with this library; if not, write to the Free Software
    Foundation, Inc., 51 Franklin Street, Fifth Floor,
    Boston, MA  02110-1335  USA

"""
import hashlib
import hmac
import os

import six

from ._cookiejar import SimpleCookieJar
from ._exceptions import *
from ._http import *
from ._logging import *
from ._socket import *

if hasattr(six, 'PY3') and six.PY3:
    from base64 import encodebytes as base64encode
else:
    from base64 import encodestring as base64encode

if hasattr(six, 'PY3') and six.PY3:
    if hasattr(six, 'PY34') and six.PY34:
        from http import client as HTTPStatus
    else:
        from http import HTTPStatus
else:
    import httplib as HTTPStatus

__all__ = ["handshake_response", "handshake", "SUPPORTED_REDIRECT_STATUSES"]

if hasattr(hmac, "compare_digest"):
    compare_digest = hmac.compare_digest
else:
    def compare_digest(s1, s2):
        return s1 == s2

# websocket supported version.
VERSION = 13

SUPPORTED_REDIRECT_STATUSES = (HTTPStatus.MOVED_PERMANENTLY, HTTPStatus.FOUND, HTTPStatus.SEE_OTHER,)
SUCCESS_STATUSES = SUPPORTED_REDIRECT_STATUSES + (HTTPStatus.SWITCHING_PROTOCOLS,)

CookieJar = SimpleCookieJar()


class handshake_response(object):

    def __init__(self, status, headers, subprotocol):
        self.status = status
        self.headers = headers
        self.subprotocol = subprotocol
        CookieJar.add(headers.get("set-cookie"))


def handshake(sock, hostname, port, resource, **options):
    headers, key = _get_handshake_headers(resource, hostname, port, options)

    header_str = "\r\n".join(headers)
    send(sock, header_str)

    status, resp = _get_resp_headers(sock)
    if status in SUPPORTED_REDIRECT_STATUSES:
        return handshake_response(status, resp, None)
    success, subproto = _validate(resp, key, options.get("subprotocols"))
    if not success:
        raise WebSocketException("Invalid WebSocket Header")

    return handshake_response(status, resp, subproto)


def _pack_hostname(hostname):
    # IPv6 address
    if ':' in hostname:
        return '[' + hostname + ']'

    return hostname

def _get_handshake_headers(resource, host, port, options):
    headers = [
        "GET %s HTTP/1.1" % resource,
        "Upgrade: websocket"
    ]
    if port == 80 or port == 443:
        hostport = _pack_hostname(host)
    else:
        hostport = "%s:%d" % (_pack_hostname(host), port)
    if "host" in options and options["host"] is not None:
        headers.append("Host: %s" % options["host"])
    else:
        headers.append("Host: %s" % hostport)

    if "suppress_origin" not in options or not options["suppress_origin"]:
        if "origin" in options and options["origin"] is not None:
            headers.append("Origin: %s" % options["origin"])
        else:
            headers.append("Origin: http://%s" % hostport)

    key = _create_sec_websocket_key()
    
    # Append Sec-WebSocket-Key & Sec-WebSocket-Version if not manually specified
    if not 'header' in options or 'Sec-WebSocket-Key' not in options['header']:
        key = _create_sec_websocket_key()
        headers.append("Sec-WebSocket-Key: %s" % key)
    else:
        key = options['header']['Sec-WebSocket-Key']

    if not 'header' in options or 'Sec-WebSocket-Version' not in options['header']:
        headers.append("Sec-WebSocket-Version: %s" % VERSION)

    if not 'connection' in options or options['connection'] is None:
        headers.append('Connection: upgrade')
    else:
        headers.append(options['connection'])

    subprotocols = options.get("subprotocols")
    if subprotocols:
        headers.append("Sec-WebSocket-Protocol: %s" % ",".join(subprotocols))

    if "header" in options:
        header = options["header"]
        if isinstance(header, dict):
            header = [
                ": ".join([k, v])
                for k, v in header.items()
                if v is not None
            ]
        headers.extend(header)

    server_cookie = CookieJar.get(host)
    client_cookie = options.get("cookie", None)

    cookie = "; ".join(filter(None, [server_cookie, client_cookie]))

    if cookie:
        headers.append("Cookie: %s" % cookie)

    headers.append("")
    headers.append("")

    return headers, key


def _get_resp_headers(sock, success_statuses=SUCCESS_STATUSES):
    status, resp_headers, status_message = read_headers(sock)
    if status not in success_statuses:
        raise WebSocketBadStatusException("Handshake status %d %s", status, status_message, resp_headers)
    return status, resp_headers


_HEADERS_TO_CHECK = {
    "upgrade": "websocket",
    "connection": "upgrade",
}


def _validate(headers, key, subprotocols):
    subproto = None
    for k, v in _HEADERS_TO_CHECK.items():
        r = headers.get(k, None)
        if not r:
            return False, None
        r = r.lower()
        if v != r:
            return False, None

    if subprotocols:
        subproto = headers.get("sec-websocket-protocol", None).lower()
        if not subproto or subproto not in [s.lower() for s in subprotocols]:
            error("Invalid subprotocol: " + str(subprotocols))
            return False, None

    result = headers.get("sec-websocket-accept", None)
    if not result:
        return False, None
    result = result.lower()

    if isinstance(result, six.text_type):
        result = result.encode('utf-8')

    value = (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode('utf-8')
    hashed = base64encode(hashlib.sha1(value).digest()).strip().lower()
    success = compare_digest(hashed, result)

    if success:
        return True, subproto
    else:
        return False, None


def _create_sec_websocket_key():
    randomness = os.urandom(16)
    return base64encode(randomness).decode('utf-8').strip()

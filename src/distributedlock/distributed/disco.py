#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""

disco.py - Remote peer discovery over kafka topic via hop-client.

This should generate events on peer changes!!!

Created on @date

@author: mlinvill
"""

import copy
import json
import time
from collections.abc import Iterator        # Python >= 3.9
from typing import Callable
import random
import pprint
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
import queue
from hop import Stream
from .lock import getmyip
from .logger import getLogger

__all__ = [
    'Disco',
    'PeerList',
    'BROKER',
    'READ_TOPIC',
    'WRITE_TOPIC',
    'NetworkError',
    'DiscoTimeoutError',
    'MissingArgumentError',
    'UnknownActionError'
]

BROKER = 'kafka.scimma.org'
READ_TOPIC = 'snews.operations'
WRITE_TOPIC = 'snews.operations'

""" We run discovery until we hear from (at least) this many peers.
"""
MIN_PEERS = 2
WATCHDOG_TIMEOUT = 600  # seconds
DISCO_STARTUP_DELAY = 30  # seconds

log = getLogger('distributed_lock')
log.setLevel(logging.DEBUG)


class NetworkError(Exception):
    """Catch-all for network problems"""


class MissingArgumentError(Exception):
    """Missing Arguments Error"""


class DiscoTimeoutError(Exception):
    """Discovery protocol timeout error"""


class UnknownActionError(Exception):
    """Discovery protocol violation"""


class Id(dict):
    """
    Inheriting from dict makes this json-serializable--required for hop messages

    Need to handle ports also.
    """

    def __init__(self):
        dict.__init__(self)
        self._myip = getmyip()

    def getmyip(self):
        """Return my ip address"""
        return self._myip


class PeerList:
    """
    Object to hold our current list of discovered peers. Trigger events on status change.

    @property doesn't play nice with set(), so we use getters/setters.

    """

    def __init__(self):
        self._length = 0
        self._state = set()
        self._callbacks = []

    def add_peer(self, peer: str) -> None:
        """Add a peer by name to the list of known peers"""
        old_state = copy.deepcopy(self._state)

        log.debug(
            "PeerList: add_peer(): Adding {peer}, state before is {pprint.pformat(self._state)}"
        )
        self._state.add(peer)
        self._length = len(self._state)
        self._notify(old_state, self.get_state())

    def remove_peer(self, peer: str) -> None:
        """Remove a peer by name from the list of known peers"""
        old_state = copy.deepcopy(self._state)

        self._state.discard(peer)
        self._length = len(self._state)
        self._notify(old_state, self.get_state())

    def register_callback(self, callback: Callable) -> None:
        """Register a function to call on peer state changes"""
        self._callbacks.append(callback)

    def deregister_callback(self, callback: Callable) -> None:
        """De-register a function previously registered to be called on peer state changes"""
        self._callbacks.remove(callback)

    def get_state(self) -> set:
        """Return the state"""
        return self._state

    def _notify(self, old_state: set, new_state: set) -> None:
        """Call the functions that registered an interest in state changes"""
        for callback in self._callbacks:
            callback(old_state, new_state)

    def __len__(self) -> int:
        """Return the number of known peers"""
        return self._length

    def __repr__(self) -> str:
        return pprint.pformat(self._state)


class Disco:
    """
        Interface to remote registration/status/discovery for distributed lock peers/network.

        TODO -
            This needs additional functionality to supervise known peers,
            to become aware of when they go away/drop off the network. Some kind of periodic
            ping/query/tickle thread. Cache?

    :return:
    """

    def __init__(self, *args, **kwargs):
        self._me = None
        self._auth = True
        self._broker = None
        self._read_topic = None
        self._write_topic = None
        self._stream_uri_r = None
        self._stream_uri_w = None
        self._stream_r = None
        self._stream_w = None
        self._in_disco = False
        self._peerlist = PeerList()
        self._id = None
        self._executor = None
        self._thrds = dict()
        self._in_queue = queue.Queue(maxsize=15)
        self._out_queue = queue.Queue(maxsize=15)
        self._event = threading.Event()
        self._endit = False

        """ setup broker, topic attributes
        """
        for k, val in kwargs.items():
            key = f"_{k}"
            self.__dict__[key] = val

        if self._broker:
            if self._read_topic:
                if "kafka://" in self._broker.lower():
                    self._stream_uri_r = f"kafka://{self._broker}/{self._read_topic}"
                else:
                    self._stream_uri_r = f"kafka://{self._broker}/{self._read_topic}"

            if self._write_topic:
                if "kafka" in self._broker.lower():
                    self._stream_uri_w = f"kafka://{self._broker}/{self._write_topic}"
                else:
                    self._stream_uri_w = f"kafka://{self._broker}/{self._write_topic}"
        else:
            raise MissingArgumentError

        # Determine my address
        self._id = Id()
        if self._id is None:
            raise NetworkError

    def __enter__(self):
        if self._stream_uri_r:
            self._stream_r = Stream(until_eos=True, auth=self._auth).open(
                self._stream_uri_r, "r"
            )

        if self._stream_uri_w:
            self._stream_w = Stream(until_eos=True, auth=self._auth).open(
                self._stream_uri_w, "w"
            )

        time.sleep(DISCO_STARTUP_DELAY)

        self._executor = ThreadPoolExecutor(max_workers=2)
        self._thrds["recv"] = self._executor.submit(self._recv, self)
        self._thrds["send"] = self._executor.submit(self._send, self)

        while not self._event.is_set():
            self.discovery()

        return self

    def __exit__(self, exception_type, exception_value, traceback) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)

        self._stream_r.close()
        self._stream_w.close()
        self._event.set()

    @staticmethod
    def _send(self) -> None:
        """Encapsulate the logic/method of actually writing"""
        while not self._event.is_set():
            for msg in [self._out_queue.get()]:
                self._stream_w.write(msg)

    @staticmethod
    def _recv(self) -> None:
        """Encapsulate the logic/method of actually reading"""
        while not self._event.is_set():
            for message in self._stream_r:
                self._in_queue.put(message)

    def discovery(self) -> None:
        """Launch the discovery protocol. Find peers."""
        if not self._in_disco:
            self._in_disco = True
            discorply = {"action": "DISCO", "source": self._id.getmyip()}
            self.produce(json.dumps(discorply))

        self.poll()

    def poll(self) -> None:
        """Main logic for the protocol, wait for messages,
           register peers, end when we have enough
        """
        while not self._endit:
            time.sleep(1 + random.randint(0, 3))

            for message in self.consume():
                msg = json.loads(message.content)

                if "END" in msg["action"]:
                    self.shutdown()
                    break

                if self._id.getmyip() == msg["source"]:
                    log.debug("skipping message from myself")
                    continue

                if "DISCO" in msg["action"]:
                    self._in_disco = True
                    self.reply()
                elif "REPLY" in msg["action"]:
                    self._peerlist.add_peer(msg["reply"])
                    if len(self._peerlist) >= MIN_PEERS:
                        self.end()
                else:
                    raise UnknownActionError

    def reply(self) -> None:
        """Reply to a discovery request"""
        rply = {
            "action": "REPLY",
            "reply": self._id.getmyip(),
            "source": self._id.getmyip(),
        }
        self.produce(json.dumps(rply))

    def end(self) -> None:
        """Send the 'end' discovery protocol action"""
        endrply = {"action": "END", "source": self._id.getmyip()}
        self.produce(json.dumps(endrply))

    def produce(self, msg: str) -> None:
        """Put msg in the work queue"""
        if not self._event.is_set() and not self._out_queue.full():
            log.debug("produce(): queueing [{msg}] for kafka")
            self._out_queue.put(str(msg))

    def consume(self) -> Iterator[json]:
        """Get messages from the work queue"""
        while not self._in_queue.empty():
            message = self._in_queue.get()
            log.debug("consume(): incoming message {message} from kafka")
            yield message

    def shutdown(self) -> None:
        """Stop disco"""
        self._endit = True
        self._event.set()

    def get_peerlist(self) -> set:
        """Return the list of peers"""
        return self._peerlist.get_state()

    def whoami(self) -> str:
        """ Return my ip address """
        return self._id.getmyip()


def watchdog_timeout() -> None:
    """Watchdog timer implementation for the discovery protocol"""
    log.error("Watchdog time-out!")
    raise DiscoTimeoutError


if __name__ == "__main__":
    watchdog = threading.Timer(WATCHDOG_TIMEOUT, watchdog_timeout)
    watchdog.daemon = True
    watchdog.start()

    with Disco(broker=BROKER, read_topic=READ_TOPIC, write_topic=WRITE_TOPIC) as disco:
        log.debug("Peers: {disco.get_peerlist()}")

    watchdog.cancel()

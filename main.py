#!/usr/bin/env python3
"""
Test implementation of a distributed lock for SNEWS Coincidence server
utilizing multiprocessing for process separation/isolation.


"""
import os
from typing import List
from time import sleep
import multiprocessing as mp
from multiprocessing import Value

from dotenv import load_dotenv
import click
from rich.console import Console
from .distributed.lock import DistributedLock, statedesc


def runlock(mynode: str, peerlist: List, leader_state: Value):
    """
    Create a DistributedLock instance and run it.

    :param mynode: str
    :param peerlist: List
    :param leader_state: Value
    :return: None
    """
    distributedlock = DistributedLock(mynode, peerlist, leader_state)
    distributedlock.run()


@click.group(invoke_without_command=True)
@click.option('--env', type=str, default='.env',
    show_default='.env', help='environment file containing the host/peer configuration')
@click.pass_context
def main(ctx=None, env=None):
    mp.set_start_method("spawn")
    MYHOSTURI = False
    peers = []

    console = Console()

    envf = env or '.env'
    load_dotenv(envf)

    if "HOSTURI" in os.environ:
        MYHOSTURI = os.environ["HOSTURI"]
    if "PEERA_URI" in os.environ:
        peers.append(os.environ["PEERA_URI"])
    if "PEERB_URI" in os.environ:
        peers.append(os.environ["PEERB_URI"])
    if "PEERC_URI" in os.environ:
        peers.append(os.environ["PEERC_URI"])


    assert MYHOSTURI is not False
    assert len(peers) > 0

    console.log(f"I am {MYHOSTURI}")
    console.log(f"peers are {peers}")

    LASTSTATE = None
    leader = mp.Value("i", 0, lock=True)

    p = mp.Process(target=runlock, args=(MYHOSTURI, peers, leader))
    p.start()

    try:
        while True:
            status = leader.value
            if LASTSTATE != status:
                console.log(f"me: {MYHOSTURI}\tstate: {statedesc[status]}")
                LASTSTATE = status

            sleep(2)

    except Exception as errmsg:
        console.print_exception()

    finally:
        p.join()


if __name__ == "__main__":
    main()

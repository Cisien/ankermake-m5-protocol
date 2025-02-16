import os
import socket
import string
import hashlib
import logging as log

from multiprocessing import Pipe
from datetime import datetime, timedelta
from threading import Thread, Event
from socket import AF_INET
from dataclasses import dataclass

from libflagship.pppp import *

PPPP_LAN_PORT = 32108
PPPP_WAN_PORT = 32100


class PPPPError(Exception):

    def __init__(self, err, message):
        self.err = err
        super().__init__(message)


@dataclass
class FileUploadInfo:
    name: str
    size: str
    md5: str
    user_name: str
    user_id: str
    machine_id: str
    type: int = 0

    @staticmethod
    def sanitize_filename(str):
        whitelist = string.ascii_letters + string.digits + "._-"

        def sanitize(c):
            if c in whitelist:
                return c
            else:
                return "_"

        cleaned = "".join(sanitize(c) for c in str)
        return cleaned.lstrip(".").replace("..", ".")

    @classmethod
    def from_file(cls, filename, user_name, user_id, machine_id, type=0):
        data = open(filename, "rb").read()
        return cls.from_data(data, filename, user_name, user_id, machine_id, type=0)

    @classmethod
    def from_data(cls, data, filename, user_name, user_id, machine_id, type=0):
        return cls(
            name=cls.sanitize_filename(os.path.basename(filename)),
            size=len(data),
            md5=hashlib.md5(data).hexdigest(),
            user_name=user_name,
            user_id=user_id,
            machine_id=machine_id,
            type=type
        )

    def __str__(self):
        return f"{self.type},{self.name},{self.size},{self.md5},{self.user_name},{self.user_id},{self.machine_id}"

    def __bytes__(self):
        return str(self).encode() + b"\x00"


class Wire:

    def __init__(self):
        self.buf = []
        self.rx, self.tx = Pipe(False)

    def read(self, size):
        while len(self.buf) < size:
            self.buf.extend(self.rx.recv())
        res, self.buf = self.buf[:size], self.buf[size:]
        return bytes(res)

    def write(self, data):
        self.tx.send(data)


class Channel:

    def __init__(self, index, max_in_flight=64):
        self.index = index
        self.rxqueue = {}
        self.txqueue = []
        self.backlog = []
        self.rx_ctr = 0
        self.tx_ctr = 0
        self.tx_ack = 0
        self.rx = Wire()
        self.tx = Wire()
        self.timeout = timedelta(seconds=0.5)
        self.acks = set()
        self.event = Event()
        self.max_in_flight = max_in_flight

    def rx_ack(self, acks):
        # remove all ACKed packets from transmission queue
        self.txqueue = [tx for tx in self.txqueue if tx[1] not in acks]

        # record any ACKs that are not yet confirmed
        for ack in acks:
            if ack >= self.tx_ack:
                self.acks.add(ack)

        # update tx_ack step by step
        while self.tx_ack in self.acks:
            self.acks.remove(self.tx_ack)
            self.tx_ack += 1

    def rx_drw(self, index, data):
        # drop any packets we have already recieved
        if self.rx_ctr > index:
            if self.rx_ctr - index > 100:
                log.warn(f"Dropping old packet: index {index} while expecting {self.rx_ctr}.")
            return

        # record packet in queue
        self.rxqueue[index] = data

        # recombine data from queue
        while self.rx_ctr in self.rxqueue:
            del self.rxqueue[self.rx_ctr]
            self.rx_ctr = (self.rx_ctr + 1) & 0xFFFF
            self.rx.write(data)

    def poll(self):
        # signal event to make blocking reads check status again
        self.event.set()

        txq = self.txqueue

        if self.backlog and len(txq) < self.max_in_flight:
            while self.backlog and len(txq) < self.max_in_flight:
                txq.append(self.backlog.pop(0))

            # sort list to make sure oldest deadline is first
            txq.sort()

        res = []
        now = datetime.now()

        while txq and txq[0][0] < now:
            deadline, index, pkt = txq.pop(0)
            res.append(PktDrw(chan=self.index, index=index, data=pkt))
            txq.append((deadline + self.timeout, index, pkt))

        # the returned chunks will be (re)transmitted
        return res

    def wait(self):
        self.event.wait()
        self.event.clear()

    def read(self, nbytes):
        return self.rx.read(nbytes)

    def write(self, payload, block=True):
        pdata = payload[:]

        tx_ctr_start = self.tx_ctr

        # schedule all packets, starting from current time
        deadline = datetime.now()
        while pdata:
            # schedule transmission in 1kb chunks
            data, pdata = pdata[:1024], pdata[1024:]
            self.backlog.append((deadline, self.tx_ctr, data))
            self.tx_ctr = (self.tx_ctr + 1) & 0xFFFF

        tx_ctr_done = self.tx_ctr

        while block:
            # if doing a blocking write, loop on self.event until we have
            # received acknowledgment of our data
            self.wait()

            if self.tx_ack >= tx_ctr_done:
                break

        return (tx_ctr_start, tx_ctr_done)


class AnkerPPPPApi(Thread):

    def __init__(self, sock, duid, addr=None):
        super().__init__()
        self.sock = sock
        self.duid = duid
        self.addr = addr

        self.new = True
        self.rdy = False
        self.chans = [Channel(n) for n in range(8)]

        self.running = True
        self.stopped = Event()

    @classmethod
    def open(cls, duid, host, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return cls(sock, duid, addr=(host, port))

    @classmethod
    def open_lan(cls, duid, host):
        return cls.open(duid, host, PPPP_LAN_PORT)

    @classmethod
    def open_wan(cls, duid, host):
        return cls.open(duid, host, PPPP_WAN_PORT)

    @classmethod
    def open_broadcast(cls):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        addr = ("255.255.255.255", PPPP_LAN_PORT)
        return cls(sock, duid=None, addr=addr)

    def stop(self):
        self.running = False
        self.stopped.wait()

    def run(self):
        log.debug("Started pppp thread")
        while self.running:
            try:
                msg = self.recv(timeout=0.05)
                self.process(msg)
            except TimeoutError:
                pass
            except StopIteration:
                break

            for idx, ch in enumerate(self.chans):
                for pkt in ch.poll():
                    self.send(pkt)

        self.send(PktClose())

        self.stopped.set()

    @property
    def host(self):
        return Host(afam=AF_INET, addr=self.addr[0], port=self.addr[1])

    def process(self, msg):

        if msg.type == Type.CLOSE:
            log.error("CLOSE")
            raise StopIteration

        elif msg.type == Type.REPORT_SESSION_READY:
            pkt = PktSessionReady(
                duid=self.duid,
                handle=-3,
                max_handles=5,
                active_handles=1,
                startup_ticks=0,
                b1=1, b2=0, b3=1, b4=0,
                addr_local=Host(afam=AF_INET, addr="0.0.0.0", port=0),
                addr_wan=Host(afam=AF_INET, addr="0.0.0.0", port=0),
                addr_relay=Host(afam=AF_INET, addr="0.0.0.0", port=0)
            )

            # self.send(pkt)

        elif msg.type == Type.ALIVE:
            self.send(PktAliveAck())

        elif msg.type == Type.DRW:
            self.send(PktDrwAck(chan=msg.chan, count=1, acks=[msg.index]))
            self.chans[msg.chan].rx_drw(msg.index, msg.data)

        elif msg.type == Type.DRW_ACK:
            self.chans[msg.chan].rx_ack(msg.acks)

        elif msg.type == Type.DEV_LGN_CRC:
            self.send(PktDevLgnAckCrc())

        elif msg.type == Type.HELLO:
            self.send(PktHelloAck(host=self.host))

        elif msg.type == Type.ALIVE_ACK:
            pass

        elif msg.type == Type.P2P_RDY:
            self.send(PktP2pRdyAck(duid=self.duid, host=self.host))

            self.new = False
            self.rdy = True

        elif msg.type == Type.PUNCH_PKT:
            if self.new:
                self.send(PktClose())
                self.send(PktP2pRdy(self.duid))

    def recv(self, timeout=None):
        self.sock.settimeout(timeout)
        data, self.addr = self.sock.recvfrom(4096)
        msg = Message.parse(data)[0]
        log.debug(f"RX <--  {msg}")
        return msg

    def send(self, pkt, addr=None):
        resp = pkt.pack()
        msg = Message.parse(resp)[0]
        log.debug(f"TX  --> {msg}")
        self.sock.sendto(resp, addr or self.addr)

    def send_xzyh(self, data, cmd, chan=0, unk0=0, unk1=0, sign_code=0, unk3=0, dev_type=0, block=True):
        xzyh = Xzyh(
            cmd=cmd,
            len=len(data),
            data=data,
            chan=chan,
            unk0=unk0,
            unk1=unk1,
            sign_code=sign_code,
            unk3=unk3,
            dev_type=dev_type
        )

        return self.chans[chan].write(xzyh.pack(), block=block)

    def send_aabb(self, data, sn=0, pos=0, frametype=0, chan=1, block=True):
        aabb = Aabb(
            frametype=frametype,
            sn=sn,
            pos=pos,
            len=len(data)
        )

        return self.chans[chan].write(aabb.pack_with_crc(data), block=block)

    def recv_xzyh(self, chan=1):
        fd = self.chans[chan]

        xzyh = Xzyh.parse(fd.read(16))[0]
        xzyh.data = fd.read(xzyh.len)
        return xzyh

    def recv_aabb(self, chan=1):
        fd = self.chans[chan]

        data = fd.read(12)
        aabb = Aabb.parse(data)[0]
        p = data + fd.read(aabb.len + 2)
        aabb, data = Aabb.parse_with_crc(p)[:2]
        return aabb, data

    def recv_aabb_reply(self, chan=1, check=True):
        aabb, data = self.recv_aabb(chan=chan)
        if len(data) != 1:
            raise ValueError(f"Unexpected reply from aabb request: {data}")

        res = FileTransferReply(data[0])
        if check and res != FileTransferReply.OK:
            raise PPPPError(res, f"Aabb request failed: {res.name}")

        return res

    def aabb_request(self, data, frametype, pos=0, chan=1, check=True):
        self.send_aabb(data=data, frametype=frametype, chan=chan, pos=pos)
        return self.recv_aabb_reply(chan, check)

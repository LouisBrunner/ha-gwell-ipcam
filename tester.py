import asyncio
import socket
from enum import IntEnum

from construct import Enum, Hex, Int32ub, Struct


def make_payload(pad_to: int, data: str) -> bytes:
    if len(data) % 2 != 0:
        msg = "Invalid data length"
        raise ValueError(msg)
    bts = bytes.fromhex(data)
    if pad_to > len(bts):
        bts += b"\x00" * (pad_to - len(bts))
    return bts


i32 = Hex(Int32ub)


class Operation(IntEnum):
    SEARCH_REQUEST = 1
    SEARCH_REPLY = 2
    ERROR = 0xFF000000


MessageHeader = Struct(
    "op" / Enum(i32, Operation),
    "unknown2" / i32,
    "unknown3" / i32,
)

# Usually
# \xff\x00\x00\x00\xff\x00\x00\x00\x10\x00\x00\x00\x00\x00\x00\x00
MessageError = Struct(
    "header" / MessageHeader,
    "unknown4" / i32,
)

MessageSearch = Struct(
    "header" / MessageHeader,
)

MessageSearchReply = Struct(
    "header" / MessageHeader,
    "unknown4" / i32,
    "camera_id" / Int32ub,
    "unknown6" / i32,
    "unknown7" / i32,
    "unknown8" / i32,
    "unknown9" / i32,
    "unknown10" / i32,
    "unknown11" / i32,
    "unknown12" / i32,
    "unknown13" / i32,
    "unknown14" / i32,
    "unknown15" / i32,
    "unknown16" / i32,
    "unknown17" / i32,
    "unknown18" / i32,
    "unknown19" / i32,
    "unknown20" / i32,
    "unknown21" / i32,
    "unknown22" / i32,
    "unknown23" / i32,
    "unknown24" / i32,
)

MessageFR = Struct(
    # unknown2: field 3 of reply
    # unknown3: field 2 of reply
    "header" / MessageHeader,
    "session_id" / i32,  # field 4 of reply
    "unknown5" / i32,  # is in reply
    "unknown6" / i32,
    "unknown7" / i32,  # is in reply
    "unknown8" / i32,
    "unknown9" / i32,  # is in reply
    "unknown10" / i32,
    "unknown11" / i32,
    "unknown12" / i32,
    "unknown13" / i32,
)


broadcast_search = {
    "header": {
        "op": Operation.SEARCH_REQUEST,
        "unknown2": 0,
        "unknown3": 28,
    }
}
first_request = {
    "header": {
        "op": 0x50038001,
        "unknown2": 0x6B000000,  # changes
        "unknown3": 0x42000000,
    },
    "session_id": 0x49FCBBAF,  # changes
    "unknown5": 0x05000000,
    "unknown6": 0,
    "unknown7": 0x04000000,
    "unknown8": 0x62D81900,  # changes
    "unknown9": 0x2D000000,
    "unknown10": 0xBD595249,  # changes
    "unknown11": 0x424AE1B7,  # changes
    "unknown12": 0xD2E57BFD,  # changes
    "unknown13": 0x1EED0F80,
}

# 500380016a0000004200000073ddffff050000000000000004000000945d14002d00000063b6f91d1aee16d1d3bfcdff1eed0f80
# 500380016b0000004200000049fcbbae050000000000000004000000f9d719002d000000d614b71fa312e12cc0ecefff1eed0f80
# 500380016b0000004200000049fcbbae05000000000000000400000062d819002d000000bd595249424ae1b7d2e57bfd1eed0f80
# 500380016b00000042000000a7fbffbf05000000000000000400000051cc02002d000000ade2f5ad2f0789a1a0dfcf4b1eed0f80
# 500380016900000042000000942bf7fe05000000000000000400000042b601002d00000035e7e81ebaad354108dabffe1eed0f80


class IPCameraServer(asyncio.DatagramProtocol):
    @staticmethod
    def send_to(
        transport: asyncio.DatagramTransport,
        msg: Struct,
        data: dict,
        addr: tuple[str, int],
    ) -> None:
        payload = msg.build(data)
        print(f">[{addr}]: {payload} <=> {msg.parse(payload)}")
        transport.sendto(payload, addr)

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        parsed = MessageHeader.parse(data)
        print(f"<[{addr}]: {data} <=> {parsed}")
        match parsed.op.intvalue:
            case Operation.ERROR:
                parsed = MessageError.parse(data)
                print(f"![{addr}] {parsed}")
            case Operation.SEARCH_REPLY:
                parsed = MessageSearchReply.parse(data)
                print(f"%[{addr}] {parsed}")
                IPCameraServer.send_to(self.transport, MessageFR, first_request, addr)


LISTEN_PORT = 43331
BROADCAST_PORT = 25143


async def run_server() -> None:
    loop = asyncio.get_running_loop()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", LISTEN_PORT))  # noqa: S104

    transport, _ = await loop.create_datagram_endpoint(IPCameraServer, sock=sock)

    IPCameraServer.send_to(
        transport, MessageSearch, broadcast_search, ("255.255.255.255", BROADCAST_PORT)
    )

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        transport.close()


asyncio.run(run_server())

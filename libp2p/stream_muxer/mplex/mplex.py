import asyncio
from typing import Dict, Optional, Tuple

from multiaddr import Multiaddr

from libp2p.network.connection.raw_connection_interface import IRawConnection
from libp2p.network.typing import GenericProtocolHandlerFn
from libp2p.peer.id import ID
from libp2p.security.secure_conn_interface import ISecureConn
from libp2p.stream_muxer.abc import IMuxedConn, IMuxedStream
from libp2p.typing import TProtocol

from .constants import HeaderTags
from .exceptions import StreamNotFound
from .mplex_stream import MplexStream
from .utils import decode_uvarint_from_stream, encode_uvarint

MPLEX_PROTOCOL_ID = TProtocol("/mplex/6.7.0")


class Mplex(IMuxedConn):
    """
    reference: https://github.com/libp2p/go-mplex/blob/master/multiplex.go
    """

    secured_conn: ISecureConn
    raw_conn: IRawConnection
    peer_id: ID
    # TODO: `dataIn` in go implementation. Should be size of 8.
    # TODO: Also, `dataIn` is closed indicating EOF in Go. We don't have similar strategies
    #   to let the `MplexStream`s know that EOF arrived (#235).
    buffers: Dict[int, "asyncio.Queue[bytes]"]
    stream_queue: "asyncio.Queue[int]"

    def __init__(
        self,
        secured_conn: ISecureConn,
        generic_protocol_handler: GenericProtocolHandlerFn,
        peer_id: ID,
    ) -> None:
        """
        create a new muxed connection
        :param secured_conn: an instance of ``ISecureConn``
        :param generic_protocol_handler: generic protocol handler
        for new muxed streams
        :param peer_id: peer_id of peer the connection is to
        """
        self.conn = secured_conn

        # Store generic protocol handler
        self.generic_protocol_handler = generic_protocol_handler

        # Set peer_id
        self.peer_id = peer_id

        # Mapping from stream ID -> buffer of messages for that stream
        self.buffers = {}

        self.stream_queue = asyncio.Queue()

        # Kick off reading
        asyncio.ensure_future(self.handle_incoming())

    @property
    def initiator(self) -> bool:
        return self.conn.initiator

    def close(self) -> None:
        """
        close the stream muxer and underlying raw connection
        """
        self.conn.close()

    def is_closed(self) -> bool:
        """
        check connection is fully closed
        :return: true if successful
        """
        raise NotImplementedError()

    async def read_buffer(self, stream_id: int) -> bytes:
        """
        Read a message from stream_id's buffer, check raw connection for new messages.
        `StreamNotFound` is raised when stream `stream_id` is not found in `Mplex`.
        :param stream_id: stream id of stream to read from
        :return: message read
        """
        if stream_id not in self.buffers:
            raise StreamNotFound(f"stream {stream_id} is not found")
        return await self.buffers[stream_id].get()

    async def read_buffer_nonblocking(self, stream_id: int) -> Optional[bytes]:
        """
        Read a message from `stream_id`'s buffer, non-blockingly.
        `StreamNotFound` is raised when stream `stream_id` is not found in `Mplex`.
        """
        if stream_id not in self.buffers:
            raise StreamNotFound(f"stream {stream_id} is not found")
        if self.buffers[stream_id].empty():
            return None
        return await self.buffers[stream_id].get()

    # FIXME: Remove multiaddr from being passed into muxed_conn
    async def open_stream(
        self, protocol_id: str, multi_addr: Multiaddr
    ) -> IMuxedStream:
        """
        creates a new muxed_stream
        :param protocol_id: protocol_id of stream
        :param multi_addr: multi_addr that stream connects to
        :return: a new stream
        """
        stream_id = self.conn.next_stream_id()
        stream = MplexStream(stream_id, True, self)
        self.buffers[stream_id] = asyncio.Queue()
        await self.send_message(HeaderTags.NewStream, None, stream_id)
        return stream

    async def accept_stream(self) -> None:
        """
        accepts a muxed stream opened by the other end
        """
        stream_id = await self.stream_queue.get()
        stream = MplexStream(stream_id, False, self)
        asyncio.ensure_future(self.generic_protocol_handler(stream))

    async def send_message(self, flag: HeaderTags, data: bytes, stream_id: int) -> int:
        """
        sends a message over the connection
        :param header: header to use
        :param data: data to send in the message
        :param stream_id: stream the message is in
        """
        # << by 3, then or with flag
        header = (stream_id << 3) | flag.value
        header = encode_uvarint(header)

        if data is None:
            data_length = encode_uvarint(0)
            _bytes = header + data_length
        else:
            data_length = encode_uvarint(len(data))
            _bytes = header + data_length + data

        return await self.write_to_stream(_bytes)

    async def write_to_stream(self, _bytes: bytearray) -> int:
        """
        writes a byte array to a raw connection
        :param _bytes: byte array to write
        :return: length written
        """
        self.conn.writer.write(_bytes)
        await self.conn.writer.drain()
        return len(_bytes)

    async def handle_incoming(self) -> None:
        """
        Read a message off of the raw connection and add it to the corresponding message buffer
        """
        # TODO Deal with other types of messages using flag (currently _)

        while True:
            stream_id, flag, message = await self.read_message()

            if stream_id is not None and flag is not None and message is not None:
                if stream_id not in self.buffers:
                    self.buffers[stream_id] = asyncio.Queue()
                    await self.stream_queue.put(stream_id)

                if flag == HeaderTags.NewStream.value:
                    # new stream detected on connection
                    await self.accept_stream()

                if message:
                    await self.buffers[stream_id].put(message)

            # Force context switch
            await asyncio.sleep(0)

    async def read_message(self) -> Tuple[int, int, bytes]:
        """
        Read a single message off of the raw connection
        :return: stream_id, flag, message contents
        """

        # FIXME: No timeout is used in Go implementation.
        # Timeout is set to a relatively small value to alleviate wait time to exit
        #  loop in handle_incoming
        timeout = 0.1
        try:
            header = await decode_uvarint_from_stream(self.conn.reader, timeout)
            length = await decode_uvarint_from_stream(self.conn.reader, timeout)
            message = await asyncio.wait_for(
                self.conn.reader.read(length), timeout=timeout
            )
        except asyncio.TimeoutError:
            return None, None, None

        flag = header & 0x07
        stream_id = header >> 3

        return stream_id, flag, message

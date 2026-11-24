#!/usr/bin/env python3
"""Run an offline V8 calculation over the 0.172.0 host's gRPC/HTTP2 stdio.

The client stays outside the empty filesystem used by static-portability.sh.
Only the exported host executes inside it. No network socket is used.
"""

import os
import select
import struct
import subprocess
import sys
import tempfile
import time
from collections import deque

import h2.config
import h2.connection
import h2.events

MAX_MESSAGE = 64 * 1024 * 1024  # code-mode-protocol/src/host.rs


class Messages:
    """Incrementally decode bounded, uncompressed gRPC message envelopes."""

    def __init__(self):
        self.buffer = bytearray()
        self.size = None

    def feed(self, data):
        self.buffer.extend(data)  # caller supplies at most one bounded HTTP/2 frame
        result = []
        while True:
            if self.size is None:
                if len(self.buffer) < 5:
                    break
                compressed, self.size = struct.unpack('>BI', self.buffer[:5])
                del self.buffer[:5]
                if compressed != 0:
                    raise ValueError('compressed gRPC response is unsupported')
                if self.size > MAX_MESSAGE:
                    raise ValueError('oversized gRPC response')
            if len(self.buffer) < self.size:
                break
            result.append(bytes(self.buffer[:self.size]))
            del self.buffer[:self.size]
            self.size = None
        return result

    def finish(self):
        if self.size is not None or self.buffer:
            raise ValueError('truncated gRPC response')


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode()
    return varint((number << 3) | 2) + varint(len(value)) + value


def fields(data):
    """Read protobuf fields; numbers below follow codex.code_mode.v1.proto."""
    offset = 0

    def integer():
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(data):
                raise ValueError('truncated protobuf varint')
            byte = data[offset]
            offset += 1
            if shift == 63 and byte > 1:
                raise ValueError('oversized protobuf varint')
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError('oversized protobuf varint')

    result = {}
    while offset < len(data):
        tag = integer()
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ValueError('invalid protobuf field')
        if wire == 0:
            value = integer()
        else:
            if wire not in (1, 2, 5):
                raise ValueError('unsupported protobuf wire type')
            size = integer() if wire == 2 else (8 if wire == 1 else 4)
            if size > len(data) - offset:
                raise ValueError('truncated protobuf field')
            value = data[offset:offset + size]
            offset += size
        result.setdefault(number, []).append(value)
    return result


def one(message, number):
    values = fields(message).get(number, [])
    if len(values) != 1 or not isinstance(values[0], bytes):
        raise ValueError('missing or invalid protobuf message field')
    return values[0]


def read_exact(stream, size, deadline):
    result = bytearray()
    while len(result) < size:
        timeout = deadline - time.monotonic()
        if timeout <= 0 or not select.select([stream], [], [], timeout)[0]:
            raise TimeoutError('code-mode host response timed out')
        chunk = os.read(stream.fileno(), size - len(result))
        if not chunk:
            raise ValueError('truncated HTTP/2 response')
        result.extend(chunk)
    return bytes(result)


def read_frame(stream, deadline, limit):
    header = read_exact(stream, 9, deadline)
    size = int.from_bytes(header[:3], 'big')
    if size > limit:
        raise ValueError('oversized HTTP/2 frame')
    return header + read_exact(stream, size, deadline)


class Client:
    def __init__(self, process, deadline):
        self.process, self.deadline = process, deadline
        self.h2 = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=True, header_encoding='utf-8'))
        self.streams = {}
        self.frames = 0
        self.h2.initiate_connection()
        self.flush()

    def flush(self):
        pending = memoryview(self.h2.data_to_send())
        while pending:
            timeout = self.deadline - time.monotonic()
            if timeout <= 0 or not select.select([], [self.process.stdin], [], timeout)[1]:
                raise TimeoutError('code-mode host request timed out')
            written = os.write(self.process.stdin.fileno(), pending)
            pending = pending[written:]

    def request(self, method, message):
        stream = self.h2.get_next_available_stream_id()
        self.streams[stream] = dict(decoder=Messages(), messages=deque(), ended=False,
                                    status=None, headers=False, count=0)
        self.h2.send_headers(stream, [(':method', 'POST'), (':scheme', 'http'),
            (':authority', 'localhost'),
            (':path', '/codex.code_mode.v1.CodeModeHost/' + method),
            ('content-type', 'application/grpc'), ('te', 'trailers')])
        self.h2.send_data(stream, struct.pack('>BI', 0, len(message)) + message,
                          end_stream=True)
        self.flush()
        return stream

    def pump(self):
        self.frames += 1
        if self.frames > 8192:
            raise ValueError('too many HTTP/2 frames')
        frame = read_frame(self.process.stdout, self.deadline, self.h2.max_inbound_frame_size)
        for event in self.h2.receive_data(frame):
            if isinstance(event, (h2.events.ConnectionTerminated, h2.events.StreamReset)):
                raise ValueError('gRPC connection or stream terminated')
            state = self.streams.get(getattr(event, 'stream_id', None))
            if isinstance(event, (h2.events.ResponseReceived, h2.events.TrailersReceived)):
                headers = dict(event.headers)
                if isinstance(event, h2.events.ResponseReceived):
                    if headers.get(':status') != '200' or not headers.get(
                            'content-type', '').startswith('application/grpc'):
                        raise ValueError('invalid gRPC response headers')
                    state['headers'] = True
                    if 'grpc-status' in headers and event.stream_ended is None:
                        raise ValueError('gRPC trailers-only response must end stream')
                if 'grpc-status' in headers:
                    state['status'] = headers['grpc-status']
                    if state['status'] != '0':
                        raise ValueError('gRPC request failed')
            elif isinstance(event, h2.events.DataReceived):
                if not state['headers']:
                    raise ValueError('gRPC data before headers')
                if state['status'] is not None:
                    raise ValueError('gRPC data after status')
                messages = state['decoder'].feed(event.data)
                state['count'] += len(messages)
                if state['count'] > 10:
                    raise ValueError('too many gRPC messages')
                state['messages'].extend(messages)
                self.h2.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
            elif isinstance(event, h2.events.StreamEnded):
                state['decoder'].finish()
                if state['status'] != '0':
                    raise ValueError('missing successful gRPC status')
                state['ended'] = True
        self.flush()

    def receive(self, stream):
        state = self.streams[stream]
        while not state['messages']:
            if state['ended']:
                raise ValueError('gRPC stream ended before expected message')
            self.pump()
        return state['messages'].popleft()

    def finish(self, stream):
        while not self.streams[stream]['ended']:
            self.pump()
        if self.streams[stream]['messages']:
            raise ValueError('unexpected extra gRPC message')


def main(command):
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=errors, bufsize=0)
        try:
            client = Client(process, time.monotonic() + 30)
            lease = client.request('OpenSession', b'')
            session_id = one(one(client.receive(lease), 1), 1)
            if not session_id:
                raise ValueError('empty session id')
            execute = client.request('Execute', b''.join([
                field(1, session_id), field(2, 'static-smoke'), field(3, 'static-smoke-1'),
                field(4, 'text("static-nix-v8-" + (6 * 7))'), field(6, 10000), field(7, 256)]))
            started = one(client.receive(execute), 1)
            assert one(started, 1) == b'static-smoke'
            outcome = one(client.receive(execute), 2)
            assert one(outcome, 1) == one(started, 2)
            completed = one(outcome, 5)
            assert not fields(completed).get(1), 'V8 execution returned an error'
            texts = [one(one(item, 1), 1) for item in fields(outcome).get(2, [])]
            assert b'static-nix-v8-42' in texts, 'V8 calculation output missing'
            client.finish(execute)  # Require successful trailers, not just matching output.
            print('V8 execution passed: static-nix-v8-42')
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('usage: static-runtime.py HOST_COMMAND [ARGS...]')
    main(sys.argv[1:])

#!/usr/bin/env python3
"""Bounded transport regressions for the real portability smoke client."""
import importlib.util
import os
from pathlib import Path
import struct
import time
import unittest
from unittest.mock import Mock, patch
from collections import deque

spec = importlib.util.spec_from_file_location('smoke', Path(__file__).with_name('static-runtime.py'))
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class FramingTests(unittest.TestCase):
    def test_fragmented_and_coalesced_messages(self):
        decoder = smoke.Messages()
        raw = b'\x00' + struct.pack('>I', 3) + b'abc' + b'\x00' + struct.pack('>I', 0)
        result = []
        for byte in raw:
            result.extend(decoder.feed(bytes([byte])))
        self.assertEqual(result, [b'abc', b''])
        decoder.finish()
        self.assertEqual(smoke.Messages().feed(raw), result)

    def test_limit_checked_before_body(self):
        with patch.object(smoke, 'MAX_MESSAGE', 8):
            self.assertEqual(smoke.Messages().feed(b'\x00' + struct.pack('>I', 8) + b'12345678'), [b'12345678'])
            with self.assertRaisesRegex(ValueError, 'oversized gRPC'):
                smoke.Messages().feed(b'\x00' + struct.pack('>I', 9))
        with self.assertRaisesRegex(ValueError, 'oversized gRPC'):
            smoke.Messages().feed(b'\x00' + struct.pack('>I', 64 * 1024 * 1024 + 1))

    def test_truncated_grpc_header_and_payload(self):
        for raw in [b'\x00', b'\x00\x00\x00\x00', b'\x00' + struct.pack('>I', 3),
                    b'\x00' + struct.pack('>I', 3) + b'ab']:
            with self.subTest(size=len(raw)):
                decoder = smoke.Messages()
                decoder.feed(raw)
                with self.assertRaisesRegex(ValueError, 'truncated gRPC'):
                    decoder.finish()

    def test_compression_rejected(self):
        with self.assertRaisesRegex(ValueError, 'compressed'):
            smoke.Messages().feed(b'\x01\x00\x00\x00\x00')

    def read_frame(self, data):
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, 'rb', buffering=0) as reader:
            with os.fdopen(write_fd, 'wb', buffering=0) as writer:
                writer.write(data)
            return smoke.read_frame(reader, time.monotonic() + 1, 16384)

    def test_http2_settings_header_not_legacy_length(self):
        header = bytes.fromhex('000012040000000000')
        self.assertEqual(struct.unpack('<I', header[:4])[0], 68288512)
        frame = header + bytes(18)
        self.assertEqual(self.read_frame(frame), frame)

    def test_oversized_http2_before_payload(self):
        with self.assertRaisesRegex(ValueError, 'oversized HTTP/2'):
            self.read_frame((16385).to_bytes(3, 'big') + bytes(6))

    def test_truncated_http2(self):
        for raw in [b'', bytes(8), b'\x00\x00\x02' + bytes(6) + b'x']:
            with self.assertRaisesRegex(ValueError, 'truncated HTTP/2'):
                self.read_frame(raw)

    def test_timeout_on_open_pipe(self):
        read_fd, write_fd = os.pipe()
        try:
            with os.fdopen(read_fd, 'rb', buffering=0) as reader:
                with self.assertRaises(TimeoutError):
                    smoke.read_frame(reader, time.monotonic() + 0.02, 16384)
        finally:
            os.close(write_fd)

    def test_protobuf_wire_types_and_truncation(self):
        self.assertEqual(smoke.fields(smoke.field(1, 'ok') + smoke.field(6, 10000)),
                         {1: [b'ok'], 6: [10000]})
        for raw in [b'\x0a\x03ab', b'\x80', b'\x00', b'\x08' + b'\xff' * 10]:
            with self.assertRaises(ValueError):
                smoke.fields(raw)

    def response_client(self, events):
        client = smoke.Client.__new__(smoke.Client)
        client.process = Mock()
        client.deadline = time.monotonic() + 1
        client.frames = 0
        client.h2 = Mock(max_inbound_frame_size=16384)
        client.h2.receive_data.return_value = events
        client.h2.data_to_send.return_value = b''
        client.streams = {1: dict(decoder=smoke.Messages(), messages=deque(),
                                  ended=False, status=None, headers=False, count=0)}
        return client

    def test_matching_output_does_not_hide_failed_or_missing_status(self):
        events = smoke.h2.events
        headers = events.ResponseReceived(stream_id=1, headers=[
            (':status', '200'), ('content-type', 'application/grpc')])
        data = b'\x00' + struct.pack('>I', 16) + b'static-nix-v8-42!'
        output = events.DataReceived(stream_id=1, data=data, flow_controlled_length=len(data))
        for trailers in [[], [events.TrailersReceived(stream_id=1, headers=[('grpc-status', '13')])]]:
            client = self.response_client([headers, output, *trailers, events.StreamEnded(stream_id=1)])
            with patch.object(smoke, 'read_frame', return_value=b''):
                with self.assertRaisesRegex(ValueError, 'gRPC'):
                    client.pump()

    def test_ok_trailers_cannot_hide_truncated_message(self):
        events = smoke.h2.events
        client = self.response_client([
            events.ResponseReceived(stream_id=1, headers=[(':status', '200'), ('content-type', 'application/grpc')]),
            events.DataReceived(stream_id=1, data=b'\x00\x00', flow_controlled_length=2),
            events.TrailersReceived(stream_id=1, headers=[('grpc-status', '0')]),
            events.StreamEnded(stream_id=1)])
        with patch.object(smoke, 'read_frame', return_value=b''):
            with self.assertRaisesRegex(ValueError, 'truncated gRPC'):
                client.pump()

    def test_initial_ok_status_cannot_replace_execute_trailers(self):
        events = smoke.h2.events
        payload = smoke.field(2, smoke.field(2, smoke.field(1, smoke.field(1, 'static-nix-v8-42')))
                              + smoke.field(5, b''))
        data = b'\x00' + struct.pack('>I', len(payload)) + payload
        client = self.response_client([
            events.ResponseReceived(stream_id=1, headers=[(':status', '200'),
                ('content-type', 'application/grpc'), ('grpc-status', '0')]),
            events.DataReceived(stream_id=1, data=data, flow_controlled_length=len(data)),
            events.StreamEnded(stream_id=1)])
        with patch.object(smoke, 'read_frame', return_value=b''):
            with self.assertRaisesRegex(ValueError, 'gRPC'):
                client.pump()

    def test_valid_trailers_only_has_no_execution_result(self):
        events = smoke.h2.events
        end = events.StreamEnded(stream_id=1)
        client = self.response_client([
            events.ResponseReceived(stream_id=1, headers=[(':status', '200'),
                ('content-type', 'application/grpc'), ('grpc-status', '0')], stream_ended=end), end])
        with patch.object(smoke, 'read_frame', return_value=b''):
            client.pump()
        self.assertTrue(client.streams[1]['ended'])
        with self.assertRaisesRegex(ValueError, 'before expected message'):
            client.receive(1)

    def test_data_after_status_is_rejected(self):
        events = smoke.h2.events
        client = self.response_client([events.DataReceived(
            stream_id=1, data=b'\x00\x00\x00\x00\x00', flow_controlled_length=5)])
        client.streams[1].update(headers=True, status='0')
        with patch.object(smoke, 'read_frame', return_value=b''):
            with self.assertRaisesRegex(ValueError, 'gRPC'):
                client.pump()


if __name__ == '__main__':
    unittest.main()

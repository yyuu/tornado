from __future__ import absolute_import, division, with_statement

import collections
import gzip
import logging
import re
import socket
from contextlib import closing
import functools

from tornado.ioloop import IOLoop
from tornado.simple_httpclient import SimpleAsyncHTTPClient, _DEFAULT_CA_CERTS
from tornado.test.httpclient_test import HTTPClientCommonTestCase, ChunkHandler, CountdownHandler, HelloWorldHandler
from tornado.testing import AsyncHTTPTestCase, LogTrapTestCase, get_unused_port
from tornado.util import b
from tornado.web import RequestHandler, Application, asynchronous, url
from tornado import netutil
from tornado.iostream import IOStream

class SimpleHTTPClientCommonTestCase(HTTPClientCommonTestCase):
    def get_http_client(self):
        client = SimpleAsyncHTTPClient(io_loop=self.io_loop,
                                       force_instance=True)
        self.assertTrue(isinstance(client, SimpleAsyncHTTPClient))
        return client

# Remove the base class from our namespace so the unittest module doesn't
# try to run it again.
del HTTPClientCommonTestCase


class TriggerHandler(RequestHandler):
    def initialize(self, queue, wake_callback):
        self.queue = queue
        self.wake_callback = wake_callback

    @asynchronous
    def get(self):
        logging.info("queuing trigger")
        self.queue.append(self.finish)
        if self.get_argument("wake", "true") == "true":
            self.wake_callback()


class HangHandler(RequestHandler):
    @asynchronous
    def get(self):
        pass


class ContentLengthHandler(RequestHandler):
    def get(self):
        self.set_header("Content-Length", self.get_argument("value"))
        self.write("ok")


class HeadHandler(RequestHandler):
    def head(self):
        self.set_header("Content-Length", "7")


class NoContentHandler(RequestHandler):
    def get(self):
        if self.get_argument("error", None):
            self.set_header("Content-Length", "7")
        self.set_status(204)


class SeeOther303PostHandler(RequestHandler):
    def post(self):
        assert self.request.body == b("blah")
        self.set_header("Location", "/303_get")
        self.set_status(303)


class SeeOther303GetHandler(RequestHandler):
    def get(self):
        assert not self.request.body
        self.write("ok")

class HostEchoHandler(RequestHandler):
    def get(self):
        self.write(self.request.headers["Host"])


class SimpleHTTPClientTestCase(AsyncHTTPTestCase, LogTrapTestCase):
    def setUp(self):
        super(SimpleHTTPClientTestCase, self).setUp()
        self.http_client = SimpleAsyncHTTPClient(self.io_loop)

    def get_app(self):
        # callable objects to finish pending /trigger requests
        self.triggers = collections.deque()
        return Application([
            url("/trigger", TriggerHandler, dict(queue=self.triggers,
                                                 wake_callback=self.stop)),
            url("/chunk", ChunkHandler),
            url("/countdown/([0-9]+)", CountdownHandler, name="countdown"),
            url("/hang", HangHandler),
            url("/hello", HelloWorldHandler),
            url("/content_length", ContentLengthHandler),
            url("/head", HeadHandler),
            url("/no_content", NoContentHandler),
            url("/303_post", SeeOther303PostHandler),
            url("/303_get", SeeOther303GetHandler),
            url("/host_echo", HostEchoHandler),
            ], gzip=True)

    def test_singleton(self):
        # Class "constructor" reuses objects on the same IOLoop
        self.assertTrue(SimpleAsyncHTTPClient(self.io_loop) is
                        SimpleAsyncHTTPClient(self.io_loop))
        # unless force_instance is used
        self.assertTrue(SimpleAsyncHTTPClient(self.io_loop) is not
                        SimpleAsyncHTTPClient(self.io_loop,
                                              force_instance=True))
        # different IOLoops use different objects
        io_loop2 = IOLoop()
        self.assertTrue(SimpleAsyncHTTPClient(self.io_loop) is not
                        SimpleAsyncHTTPClient(io_loop2))

    def test_connection_limit(self):
        client = SimpleAsyncHTTPClient(self.io_loop, max_clients=2,
                                       force_instance=True)
        self.assertEqual(client.max_clients, 2)
        seen = []
        # Send 4 requests.  Two can be sent immediately, while the others
        # will be queued
        for i in range(4):
            client.fetch(self.get_url("/trigger"),
                         lambda response, i=i: (seen.append(i), self.stop()))
        self.wait(condition=lambda: len(self.triggers) == 2)
        self.assertEqual(len(client.queue), 2)

        # Finish the first two requests and let the next two through
        self.triggers.popleft()()
        self.triggers.popleft()()
        self.wait(condition=lambda: (len(self.triggers) == 2 and
                                     len(seen) == 2))
        self.assertEqual(set(seen), set([0, 1]))
        self.assertEqual(len(client.queue), 0)

        # Finish all the pending requests
        self.triggers.popleft()()
        self.triggers.popleft()()
        self.wait(condition=lambda: len(seen) == 4)
        self.assertEqual(set(seen), set([0, 1, 2, 3]))
        self.assertEqual(len(self.triggers), 0)

    def test_redirect_connection_limit(self):
        # following redirects should not consume additional connections
        client = SimpleAsyncHTTPClient(self.io_loop, max_clients=1,
                                       force_instance=True)
        client.fetch(self.get_url('/countdown/3'), self.stop,
                     max_redirects=3)
        response = self.wait()
        response.rethrow()

    def test_default_certificates_exist(self):
        open(_DEFAULT_CA_CERTS).close()

    def test_gzip(self):
        # All the tests in this file should be using gzip, but this test
        # ensures that it is in fact getting compressed.
        # Setting Accept-Encoding manually bypasses the client's
        # decompression so we can see the raw data.
        response = self.fetch("/chunk", use_gzip=False,
                              headers={"Accept-Encoding": "gzip"})
        self.assertEqual(response.headers["Content-Encoding"], "gzip")
        self.assertNotEqual(response.body, b("asdfqwer"))
        # Our test data gets bigger when gzipped.  Oops.  :)
        self.assertEqual(len(response.body), 34)
        f = gzip.GzipFile(mode="r", fileobj=response.buffer)
        self.assertEqual(f.read(), b("asdfqwer"))

    def test_max_redirects(self):
        response = self.fetch("/countdown/5", max_redirects=3)
        self.assertEqual(302, response.code)
        # We requested 5, followed three redirects for 4, 3, 2, then the last
        # unfollowed redirect is to 1.
        self.assertTrue(response.request.url.endswith("/countdown/5"))
        self.assertTrue(response.effective_url.endswith("/countdown/2"))
        self.assertTrue(response.headers["Location"].endswith("/countdown/1"))

    def test_303_redirect(self):
        response = self.fetch("/303_post", method="POST", body="blah")
        self.assertEqual(200, response.code)
        self.assertTrue(response.request.url.endswith("/303_post"))
        self.assertTrue(response.effective_url.endswith("/303_get"))
        #request is the original request, is a POST still
        self.assertEqual("POST", response.request.method)

    def test_request_timeout(self):
        response = self.fetch('/trigger?wake=false', request_timeout=0.1)
        self.assertEqual(response.code, 599)
        self.assertTrue(0.099 < response.request_time < 0.11, response.request_time)
        self.assertEqual(str(response.error), "HTTP 599: Timeout")
        # trigger the hanging request to let it clean up after itself
        self.triggers.popleft()()

    def test_ipv6(self):
        if not socket.has_ipv6:
            # python compiled without ipv6 support, so skip this test
            return
        try:
            self.http_server.listen(self.get_http_port(), address='::1')
        except socket.gaierror, e:
            if e.args[0] == socket.EAI_ADDRFAMILY:
                # python supports ipv6, but it's not configured on the network
                # interface, so skip this test.
                return
            raise
        url = self.get_url("/hello").replace("localhost", "[::1]")

        # ipv6 is currently disabled by default and must be explicitly requested
        self.http_client.fetch(url, self.stop)
        response = self.wait()
        self.assertEqual(response.code, 599)

        self.http_client.fetch(url, self.stop, allow_ipv6=True)
        response = self.wait()
        self.assertEqual(response.body, b("Hello world!"))

    def test_multiple_content_length_accepted(self):
        response = self.fetch("/content_length?value=2,2")
        self.assertEqual(response.body, b("ok"))
        response = self.fetch("/content_length?value=2,%202,2")
        self.assertEqual(response.body, b("ok"))

        response = self.fetch("/content_length?value=2,4")
        self.assertEqual(response.code, 599)
        response = self.fetch("/content_length?value=2,%202,3")
        self.assertEqual(response.code, 599)

    def test_head_request(self):
        response = self.fetch("/head", method="HEAD")
        self.assertEqual(response.code, 200)
        self.assertEqual(response.headers["content-length"], "7")
        self.assertFalse(response.body)

    def test_no_content(self):
        response = self.fetch("/no_content")
        self.assertEqual(response.code, 204)
        # 204 status doesn't need a content-length, but tornado will
        # add a zero content-length anyway.
        self.assertEqual(response.headers["Content-length"], "0")

        # 204 status with non-zero content length is malformed
        response = self.fetch("/no_content?error=1")
        self.assertEqual(response.code, 599)

    def test_host_header(self):
        host_re = re.compile(b("^localhost:[0-9]+$"))
        response = self.fetch("/host_echo")
        self.assertTrue(host_re.match(response.body))

        url = self.get_url("/host_echo").replace("http://", "http://me:secret@")
        self.http_client.fetch(url, self.stop)
        response = self.wait()
        self.assertTrue(host_re.match(response.body), response.body)

    def test_100_continue(self):
        # testing if httpclient is able to skip 100 continue responses.
        # to test without httpserver implementation, using
        # raw response as same as httpclient_test.test_chunked_close.
        port = get_unused_port()
        (sock,) = netutil.bind_sockets(port, address="127.0.0.1")
        with closing(sock):
            def write_response(stream, request_data):
                stream.write(b("""\
HTTP/1.1 100 Continue

HTTP/1.1 200 OK
Content-Length: 6

hjkl
""").replace(b("\n"), b("\r\n")), callback=stream.close)
            def accept_callback(conn, address):
                # fake an HTTP server using chunked encoding where the final chunks
                # and connection close all happen at once
                stream = IOStream(conn, io_loop=self.io_loop)
                stream.read_until(b("\r\n\r\n"),
                                  functools.partial(write_response, stream))
            netutil.add_accept_handler(sock, accept_callback, self.io_loop)
            self.http_client.fetch("http://127.0.0.1:%d/" % port, self.stop,
                                   headers={"Expect": "100-continue"})
            resp = self.wait()
            resp.rethrow()
            self.assertEqual(resp.body, b("hjkl\r\n"))


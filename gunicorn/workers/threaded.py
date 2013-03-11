# -*- coding: utf-8 -
#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.
#

from datetime import datetime
import errno
import os
import select
import socket
import ssl
import threading
import Queue

import gunicorn.http as http
import gunicorn.http.wsgi as wsgi
import gunicorn.util as util
import gunicorn.workers.base as base
from gunicorn import six


lock = threading.Lock()


class ServingThread(threading.Thread):

    def __init__(self, queue, worker):
        super(ServingThread, self).__init__()
        self.queue = queue
        self.worker = worker
        self.daemon = True

    def run(self):
        # conventionally all gunicorn worker logic resides in the
        # worker.  this is acceptable as the only concurrent workers
        # parallelize on socket calls, so there is no "critical" code
        #
        # i abstraced out as much as possible from the sync worker
        # into this thread subclass.  critical sections are markedre
        # (and hopefully protected by a lock), while the worker
        # methods that remain appeared to contain no critical code.
        
        while True:
            req = None
            try:
                listener, client, addr = self.queue.get(block=True)

                if self.worker.cfg.is_ssl:
                    client = ssl.wrap_socket(client, server_side=True,
                                             do_handshake_on_connect=False,
                                             **self.worker.cfg.ssl_options)
                
                parser = http.RequestParser(self.worker.cfg, client)
                req = six.next(parser)
                self.handle_request(listener, req, client, addr)
            except http.errors.NoMoreData as e:
                self.worker.log.debug("Ignored premature client "
                                      "disconnection. %s", e)
            except StopIteration as e:
                self.worker.log.debug("Closing connection. %s", e)
            except ssl.SSLError as e:
                if e.args[0] == ssl.SSL_ERROR_EOF:
                    self.worker.log.debug("ssl connection closed")
                    client.close()
                else:
                    self.worker.log.debug("Error processing SSL request.")
                    self.worker.handle_error(req, client, addr, e)
            except socket.error as e:
                if e.args[0] != errno.EPIPE:
                    self.worker.log.exception("Error processing request.")
                else:
                    self.worker.log.debug("Ignoring EPIPE")
            except Exception as e:
                self.worker.handle_error(req, client, addr, e)
            finally:
                self.queue.task_done()
                util.close(client)


    def handle_request(self, listener, req, client, addr):
        environ = {}
        resp = None
        try:
            self.worker.cfg.pre_request(self.worker, req)
            request_start = datetime.now()
            resp, environ = wsgi.create(req, client, addr,
                    listener.getsockname(), self.worker.cfg)
            # Force the connection closed until someone shows
            # a buffering proxy that supports Keep-Alive to

            # the backend.
            resp.force_close()

            with lock:
                self.worker.nr += 1 # CRITICAL
                if self.worker.nr >= self.worker.max_requests: # CRITICAL
                    self.worker.log.info("Autorestarting worker "
                                         "after current request.")
                    self.worker.alive = False # CRITICAL
                    
            respiter = self.worker.wsgi(environ, resp.start_response)
            try:
                if isinstance(respiter, environ['wsgi.file_wrapper']):
                    resp.write_file(respiter)
                else:
                    for item in respiter:
                        resp.write(item)
                resp.close()
                request_time = datetime.now() - request_start
                self.worker.log.access(resp, req, environ, request_time)
            finally:
                if hasattr(respiter, "close"):
                    respiter.close()
        except socket.error:
            raise
        except Exception as e:
            # Only send back traceback in HTTP in debug mode.
            self.worker.handle_error(req, client, addr, e)
            return
        finally:
            try:
                self.worker.cfg.post_request(self, req, environ, resp)
            except Exception:
                self.worker.log.exception("Exception in post_request hook")



class ThreadedWorker(base.Worker):

    def __init__(self, *args, **kwargs):
        super(ThreadedWorker, self).__init__(*args, **kwargs)
        self.worker_connections = self.cfg.worker_connections

    def run(self):
        # self.socket appears to lose its blocking status after
        # we fork in the arbiter. Reset it here.
        [s.setblocking(0) for s in self.sockets]

        connection_queue = Queue.Queue()
        thread_pool = []

        for _ in xrange(self.worker_connections):
            st = ServingThread(connection_queue, self)
            st.start()
            thread_pool.append(st)
        
        ready = self.sockets
        while self.alive:
            self.notify()

            # Accept a connection. If we get an error telling us
            # that no connection is waiting we fall down to the
            # select which is where we'll wait for a bit for new
            # workers to come give us some love.

            for sock in ready:
                try:
                    client, addr = sock.accept()
                    client.setblocking(1)
                    util.close_on_exec(client)
                    connection_queue.put((sock, client, addr),
                                         block=True)

                    # Keep processing clients until no one is waiting. This
                    # prevents the need to select() for every client that we
                    # process.
                    continue

                except socket.error as e:
                    if e.args[0] not in (errno.EAGAIN, errno.ECONNABORTED,
                            errno.EWOULDBLOCK):
                        raise

            # If our parent changed then we shut down.
            if self.ppid != os.getppid():
                self.log.info("Parent changed, shutting down: %s", self)
                return

            try:
                self.notify()
                ret = select.select(self.sockets, [], self.PIPE, self.timeout)
                if ret[0]:
                    ready = ret[0]
                    continue
            except select.error as e:
                if e.args[0] == errno.EINTR:
                    ready = self.sockets
                    continue
                if e.args[0] == errno.EBADF:
                    if self.nr < 0:
                        ready = self.sockets
                        continue
                    else:
                        return
                raise

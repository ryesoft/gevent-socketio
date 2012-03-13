import random
import weakref

import gevent
from gevent.queue import Queue
from gevent.event import Event

from socketio import packet

def default_error_handler(socket, error_name, error_message, endpoint, msg_id,
                          quiet):
    """This is the default error handler, you can override this by [TODO: INSERT
    HOW HERE].

    It basically sends an event through the socket with the 'error' name.

    See documentation for Socket.error().

    ``quiet``, if quiet, this handler will not send a packet to the user, but
               only log for the server developer.
    """
    pkt = dict(type='event', name='error',
               args=[error_name, error_message])
    if endpoint:
        pkt['endpoint'] = endpoint
    if msg_id:
        pkt['id'] = msg_id
    
    # Send an error event through the Socket
    if not quiet:
        socket.send_packet(pkt)

    # Log that error somewhere for debugging...
    print "default_error_handler: %s, %s (endpoint=%s, msg_id=%s)" % (
        error_name, error_message, endpoint, msg_id)


class Socket(object):
    """
    Virtual Socket implementation, checks heartbeats, writes to local queues for
    message passing, holds the Namespace objects, dispatches de packets to the
    underlying namespaces.

    This is the abstraction on top of the different transports.  It's like
    if you used a WebSocket only...
    """

    error_handler = default_error_handler
    """You can attach a different error_handler. Look at default_error_handler
    for inspiration.
    """

    STATE_NEW = "NEW"
    STATE_CONNECTED = "CONNECTED"
    STATE_DISCONNECTING = "DISCONNECTING"
    STATE_DISCONNECTED = "DISCONNECTED"

    GLOBAL_NS = None
    """Use this to be explicit when specifying a Global Namespace (an endpoint
    with no name, not '/chat' or anything."""

    def __init__(self, server):
        self.server = weakref.proxy(server)
        self.sessid = str(random.random())[2:]
        self.client_queue = Queue() # queue for messages to client
        self.server_queue = Queue() # queue for messages to server
        self.hits = 0
        self.heartbeats = 0
        self.timeout = Event()
        self.wsgi_app_greenlet = None
        self.state = "NEW"
        self.connection_confirmed = False
        self.ack_callbacks = {}
        self.request = None
        self.environ = None
        self.namespaces = {}
        self.active_ns = {} # Namespace sessions that were instantiated (/chat)
        self.jobs = []

    def _set_namespaces(self, namespaces):
        """This is a mapping (dict) of the different '/namespaces' to their
        BaseNamespace object derivative.
        
        This is called by socketio_manage()."""
        self.namespaces = namespaces

    def _set_request(self, request):
        """Saves the request object for future use by the different Namespaces.

        This is called by socketio_manage().
        """
        self.request = request
    
    def _set_environ(self, environ):
        """Save the WSGI environ, for future use.

        This is called by socketio_manage().
        """
        self.environ = environ

    def __str__(self):
        result = ['sessid=%r' % self.sessid]
        if self.state == self.STATE_CONNECTED:
            result.append('connected')
        if self.client_queue.qsize():
            result.append('client_queue[%s]' % self.client_queue.qsize())
        if self.server_queue.qsize():
            result.append('server_queue[%s]' % self.server_queue.qsize())
        if self.hits:
            result.append('hits=%s' % self.hits)
        if self.heartbeats:
            result.append('heartbeats=%s' % self.heartbeats)
        return ' '.join(result)


    def __getitem__(self, key):
        """This will get the nested Namespace using its '/chat' reference.

        Using this, you can go from one Namespace to the other (to emit, add
        ACLs, etc..) with:

          adminnamespace.socket['/chat'].add_acl_method('kick-ban')

        """
        return self.active_ns[key]

    def __hasitem__(self, key):
        """Verifies if the namespace is active (was initialized)"""
        return key in self.active_ns

    @property
    def connected(self):
        return self.state == self.STATE_CONNECTED

    def incr_hits(self):
        self.hits += 1

        if self.hits == 1:
            self.state = self.STATE_CONNECTED

    def heartbeat(self):
        """This makes the heart beat for another X seconds.  Call this when
        you get a heartbeat packet in.

        This clear the heartbeat disconnect timeout (resets for X seconds).
        """
        self.timeout.set()

    def kill(self):
        """This function must / will be called when a socket is to be completely
        shut down, closed by connection timeout, connection error or explicit
        disconnection from the client.

        It will call all of the namespaces' disconnect() methods so that you
        can shut-down things properly.
        """
        if self.connected:
            self.state = self.STATE_DISCONNECTING
            self.server_queue.put_nowait(None)
            self.client_queue.put_nowait(None)
            self.disconnect()
            #gevent.kill(self.wsgi_app_greenlet)
        else:
            pass # Fail silently

    def put_server_msg(self, msg):
        """Used by the transports"""
        self.heartbeat()
        self.server_queue.put_nowait(msg)

    def put_client_msg(self, msg):
        """Used by the transports"""
        self.heartbeat()
        self.client_queue.put_nowait(msg)

    def get_client_msg(self, **kwargs):
        """Used by the transports"""
        return self.client_queue.get(**kwargs)

    def get_server_msg(self, **kwargs):
        """Used by the transports"""
        return self.server_queue.get(**kwargs)


    def error(self, error_name, error_message, endpoint=None, msg_id=None,
              quiet=False):
        """Send an error to the user, using the custom or default
        ErrorHandler configured on the [TODO: Revise this] Socket/Handler
        object.

        ``error_name`` is a simple string, for easy association on the client
                       side
        ``error_message`` is a human readable message, the user will eventually
                          see
        ``endpoint`` set this if you have a message specific to an end point
        ``msg_id`` set this if your error is relative to a specific message
        ``quiet`` a way to make the error handler quiet. Specific to the handler.
                  The default handler will not send a message to the user, but
                  only log.
        """
        self.error_handler(self, error_name, error_message, endpoint, msg_id,
                           quiet)

    # User facing low-level function
    def disconnect(self):
        """Calling this method will call the disconnect() method on all the
        active Namespaces that were open, and remove them from the ``active_ns``
        map.
        """
        for ns_name, ns in self.active_ns.iteritems():
            ns.disconnect()
        # TODO: Find a better way to remove the Namespaces from the ``active_ns``
        #       zone.  Have the Ns.disconnect() call remove itself from the
        #       underlying socket ?
        self.active_ns = {}

    def send_packet(self, pkt):
        """Low-level interface to queue a packet on the wire (encoded as wire
        protocol"""
        self.put_client_msg(packet.encode(pkt))

    def spawn(self, fn, *args, **kwargs):
        """Spawn a new Greenlet, attached to this Socket instance.

        It will be monitored by the "watcher" method
        """

        self.debug("Spawning sub-Socket Greenlet: %s" % fn.__name__)
        job = gevent.spawn(fn, *args, **kwargs)
        self.jobs.append(job)
        return job

    def _receiver_loop(self):
        """This is the loop that takes messages from the queue for the server
        to consume, decodes them and dispatches them.
        """

        while True:
            rawdata = self.get_server_msg()

            if rawdata:
                try:
                    pkt = packet.decode(rawdata)
                except (ValueError, KeyError, Exception), e:
                    self.error('invalid_packet', "There was a decoding error when dealing with packet with event: %s" % rawdata[:15])
                    continue

                if pkt['type'] == 'heartbeat':
                    # This is already dealth with in put_server_msg() when
                    # any incoming raw data arrives.
                    continue

                endpoint = pkt['endpoint']

                if endpoint not in self.namespaces:
                    self.error("no_such_namespace", "The endpoint you tried to connect to doesn't exist: %s" % endpoint, endpoint=endpoint)
                    continue
                elif endpoint in self.active_ns:
                    pkt_ns = self.active_ns[endpoint]
                else:
                    new_ns_class = self.namespaces[endpoint]
                    pkt_ns = new_ns_class(self.environ, endpoint,
                                               request=self.request)
                    if not pkt_ns.connect():
                        self.error("namespace_connect_error", "Connection error to endpoint: %s" % endpoint or 'GLOBAL', endpoint=endpoint or 'GLOBAL')
                        continue
                    self.active_ns[endpoint] = pkt_ns

                pkt_ns.process_packet(pkt)

            if not self.connected:
                self.kill() # ?? what,s the best clean-up when its not a
                            # user-initiated disconnect
                return

    def _spawn_receiver_loop(self):
        """Spawns the reader loop.  This is called internall by socketio_manage()
        """
        job = gevent.spawn(self._receiver_loop)
        self.jobs.append(job)
        return job

    def _watcher(self):
        """Watch if any of the greenlets for a request have died. If so, kill the
        request and the socket.
        """
        # TODO: add that if any of the request.jobs die, kill them all and exit
        gevent.sleep(5.0)

        while True:
            gevent.sleep(1.0)

            if not self.connected:
                # Killing Socket-level jobs
                gevent.killall(self.jobs)
                for ns_name, ns in self.active_ns:
                    ns.disconnect()
                    ns.kill_local_jobs()

    def _spawn_watcher(self):
        job = gevent.spawn(self._watcher)
        return job
    
    def _heartbeat(self):
        """Start the heartbeat Greenlet to check connection health."""
        self.state = self.STATE_CONNECTED

        while self.connected:
            gevent.sleep(5.0) # FIXME: make this a setting
            # TODO: this process could use a timeout object like the disconnect
            #       timeout thing, and ONLY send packets when none are sent!
            #       We would do that by calling timeout.set() for a "sending"
            #       timeout.  If we're sending 100 messages a second, there is
            #       no need to push some heartbeats in there also.
            self.put_client_msg("2::") # TODO: make it a heartbeat packet

    def _disconnect_timeout(self):
        self.timeout.clear()

        if self.timeout.wait(10.0):
            gevent.spawn(self._disconnect_timeout)
        else:
            self.kill()

    def _spawn_heartbeat(self):
        """This functions returns a list of jobs"""
        job_sender = gevent.spawn(self._heartbeat)
        job_waiter = gevent.spawn(self._disconnect_timeout)
        self.jobs.extend((job_sender, job_waiter))
        return job_sender, job_waiter
################################################################################
#
#  Copyright 2014-2016 Eric Lacombe <eric.lacombe@security-labs.org>
#
################################################################################
#
#  This file is part of fuddly.
#
#  fuddly is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  fuddly is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with fuddly. If not, see <http://www.gnu.org/licenses/>
#
################################################################################

from __future__ import print_function

import os
import random
import subprocess
import fcntl
import select
import signal
import datetime
import socket
import threading
import copy
import struct
import time
import collections
import binascii

import errno
from socket import error as socket_error

from uuid import getnode

from libs.external_modules import *
from framework.data_model import Data, NodeSemanticsCriteria
from framework.value_types import GSMPhoneNum
from framework.global_resources import *

class TargetStuck(Exception): pass

class Target(object):
    '''
    Class abstracting the target we interact with.
    '''
    feedback_timeout = None
    _logger = None
    _probes = None
    _send_data_lock = threading.Lock()

    def __init__(self):
        '''
        To be overloaded if needed
        '''
        pass

    def set_logger(self, logger):
        self._logger = logger

    def set_data_model(self, dm):
        self.current_dm = dm

    def _start(self):
        self._logger.print_console('*** Target initialization ***\n', nl_before=False, rgb=Color.COMPONENT_START)
        return self.start()

    def _stop(self):
        self._logger.print_console('*** Target cleanup procedure ***\n', nl_before=False, rgb=Color.COMPONENT_STOP)
        return self.stop()

    def start(self):
        '''
        To be overloaded if needed
        '''
        return True

    def stop(self):
        '''
        To be overloaded if needed
        '''
        return True

    def record_info(self, info):
        """
        Can be used by the target to record some information during initialization or anytime
        it make sense for your purpose.

        Args:
            info (str): info to be recorded

        Returns:
            None
        """
        self._logger.log_comment(info)

    def send_data(self, data, from_fmk=False):
        '''
        To be overloaded.

        Note: use data.to_bytes() to get binary data.

        Args:
          from_fmk (bool): set to True if the call was performed by the framework itself,
            otherwise the call comes from user-code (e.g., from a `probe` or an `operator`)
          data (Data): data container that embeds generally a
            modeled data accessible through `data.node`. However if the
            latter is None, it only embeds the raw data.
        '''
        raise NotImplementedError

    def send_multiple_data(self, data_list, from_fmk=False):
        '''
        Used to send multiple data to the target, or to stimulate several
        target's inputs in one shot.

        Note: Use data.to_bytes() to get binary data

        Args:
            from_fmk (bool): set to True if the call was performed by the framework itself,
              otherwise the call comes from user-code (e.g., from a `Probe` or an `Operator`)
            data_list (list): list of data to be sent

        '''
        raise NotImplementedError


    def is_target_ready_for_new_data(self):
        '''
        The FMK busy wait on this method() before sending a new data.
        This method should take into account feedback timeout (that is the maximum
        time duration for gathering feedback from the target)
        '''
        return True

    def get_last_target_ack_date(self):
        '''
        If different from None the return value is used by the FMK to log the
        date of the target acknowledgment after a message has been sent to it.

        [Note: If this method is overloaded, is_target_ready_for_new_data() should also be]
        '''
        return None

    def cleanup(self):
        '''
        To be overloaded if something needs to be performed after each data emission.
        It is called after any feedback has been retrieved.
        '''
        pass

    def recover_target(self):
        '''
        Implementation of target recovering operations, when a target problem has been detected
        (i.e. a negative feedback from a probe, an operator or the Target() itself)

        Returns:
            bool: True if the target has been recovered. False otherwise.
        '''
        raise NotImplementedError

    def get_feedback(self):
        '''
        If overloaded, should return a TargetFeedback object.
        '''
        return None

    def collect_feedback_without_sending(self):
        """
        If overloaded, it can be used by the framework to retrieve additional feedback from the
        target without sending any new data.

        Returns:
            bool: False if it is not possible, otherwise it should be True
        """
        return True

    def set_feedback_timeout(self, fbk_timeout):
        '''
        To set dynamically the feedback timeout.

        Args:
            fbk_timeout: maximum time duration for collecting the feedback

        '''
        assert fbk_timeout >= 0
        self.feedback_timeout = fbk_timeout
        self._set_feedback_timeout_specific(fbk_timeout)

    def _set_feedback_timeout_specific(self, fbk_timeout):
        '''
        Overload this function to handle feedback specifics

        Args:
            fbk_timeout: time duration for collecting the feedback

        '''
        pass

    def get_description(self):
        return None

    def send_data_sync(self, data, from_fmk=False):
        '''
        Can be used in user-code to send data to the target without interfering
        with the framework.

        Use case example: The user needs to send some message to the target on a regular basis
        in background. For that purpose, it can quickly define a :class:`framework.monitor.Probe` that just
        emits the message by itself.
        '''
        with self._send_data_lock:
            self.send_data(data, from_fmk=from_fmk)

    def send_multiple_data_sync(self, data_list, from_fmk=False):
        '''
        Can be used in user-code to send data to the target without interfering
        with the framework.
        '''
        with self._send_data_lock:
            self.send_multiple_data(data_list, from_fmk=from_fmk)

    def add_probe(self, probe):
        if self._probes is None:
            self._probes = []
        self._probes.append(probe)

    def remove_probes(self):
        self._probes = None

    @property
    def probes(self):
        return self._probes if self._probes is not None else []


class TargetFeedback(object):
    fbk_lock = threading.Lock()

    def __init__(self, bstring=b''):
        self.cleanup()
        self._feedback_collector = collections.OrderedDict()
        self._feedback_collector_tstamped = collections.OrderedDict()
        self.set_bytes(bstring)

    def add_fbk_from(self, ref, fbk):
        now = datetime.datetime.now()
        with self.fbk_lock:
            if ref not in self._feedback_collector:
                self._feedback_collector[ref] = []
                self._feedback_collector_tstamped[ref] = []
            if fbk.strip() not in self._feedback_collector[ref]:
                self._feedback_collector[ref].append(fbk)
                self._feedback_collector_tstamped[ref].append(now)

    def has_fbk_collector(self):
        return len(self._feedback_collector) > 0

    def __iter__(self):
        with self.fbk_lock:
            fbk_collector = copy.copy(self._feedback_collector)
            fbk_collector_ts = copy.copy(self._feedback_collector_tstamped)
        for ref, fbk_list in fbk_collector.items():
            yield ref, fbk_list, fbk_collector_ts[ref]

    def iter_and_cleanup_collector(self):
        with self.fbk_lock:
            fbk_collector = self._feedback_collector
            fbk_collector_ts = self._feedback_collector_tstamped
            self._feedback_collector = collections.OrderedDict()
            self._feedback_collector_tstamped = collections.OrderedDict()
        for ref, fbk_list in fbk_collector.items():
            yield ref, fbk_list, fbk_collector_ts[ref]

    def set_error_code(self, err_code):
        self._err_code = err_code

    def get_error_code(self):
        return self._err_code

    def set_bytes(self, bstring):
        now = datetime.datetime.now()
        self._tstamped_bstring = (bstring, now)

    def get_bytes(self):
        return None if self._tstamped_bstring is None else self._tstamped_bstring[0]

    def get_timestamp(self):
        return None if self._tstamped_bstring is None else self._tstamped_bstring[1]

    def cleanup(self):
        # collector cleanup is done during consumption to avoid loss of feedback in
        # multi-threading context
        self._tstamped_bstring = None
        self.set_error_code(0)


class EmptyTarget(Target):

    def send_data(self, data, from_fmk=False):
        pass

    def send_multiple_data(self, data_list, from_fmk=False):
        pass


class NetworkTarget(Target):
    '''Generic target class for interacting with a network resource. Can
    be used directly, but some methods may require to be overloaded to
    fit your needs.
    '''

    UNKNOWN_SEMANTIC = 42
    CHUNK_SZ = 2048
    _INTERNALS_ID = 'NetworkTarget()'

    def __init__(self, host='localhost', port=12345, socket_type=(socket.AF_INET, socket.SOCK_STREAM),
                 data_semantics=UNKNOWN_SEMANTIC, server_mode=False, hold_connection=False,
                 mac_src=None, mac_dst=None):
        '''
        Args:
          host (str): the IP address of the target to connect to, or
            the IP address on which we will wait for target connecting
            to us (if `server_mode` is True).
          port (int): the port for communicating with the target, or
            the port to listen to.
          socket_type (tuple): tuple composed of the socket address family
            and socket type
          data_semantics (str): string of characters that will be used for
            data routing decision. Useful only when more than one interface
            are defined. In such case, the data semantics will be checked in
            order to find a matching interface to which data will be sent. If
            the data have no semantic, it will be routed to the default first
            declared interface.
          server_mode (bool): If `True`, the interface will be set in server mode,
            which means we will wait for the real target to connect to us for sending
            it data.
          hold_connection (bool): If `True`, we will maintain the connection while
            sending data to the real target. Otherwise, after each data emission,
            we close the related socket.
        '''

        if not self._is_valid_socket_type(socket_type):
            raise ValueError("Unrecognized socket type")

        self._mac_src = struct.pack('>Q', getnode())[2:] if mac_src is None else mac_src
        self._mac_dst = mac_dst
        self._mac_src_semantic = NodeSemanticsCriteria(mandatory_criteria=['mac_src'])
        self._mac_dst_semantic = NodeSemanticsCriteria(mandatory_criteria=['mac_dst'])

        self._host = {}
        self._port = {}
        self._socket_type = {}
        self.host = self._host[self.UNKNOWN_SEMANTIC] = self._host[data_semantics] = host
        self.port = self._port[self.UNKNOWN_SEMANTIC] = self._port[data_semantics] = port
        self._socket_type[self.UNKNOWN_SEMANTIC] = self._socket_type[data_semantics] = socket_type

        self.known_semantics = []
        self.sending_sockets = []
        self.multiple_destination = False

        self._feedback = TargetFeedback()

        self._fbk_handling_lock = threading.Lock()
        self.socket_desc_lock = threading.Lock()

        self.set_timeout(fbk_timeout=6, sending_delay=4)

        self.feedback_length = None  # if specified, timeout will be ignored

        self._default_fbk_socket_id = 'Default Feedback Socket'
        self._default_fbk_id = {}
        self._additional_fbk_desc = {}
        self._default_additional_fbk_id = 1

        self._default_fbk_id[(host, port)] = self._default_fbk_socket_id + ' - {:s}:{:d}'.format(host, port)

        self.server_mode = {}
        self.server_mode[(host,port)] = server_mode
        self.hold_connection = {}
        self.hold_connection[(host, port)] = hold_connection

        self.stop_event = threading.Event()
        self._server_thread_lock = threading.Lock()


    def _is_valid_socket_type(self, socket_type):
        skt_sz = len(socket_type)
        if skt_sz == 3:
            family, sock_type, proto = socket_type
            if sock_type != socket.SOCK_RAW:
                return False
        elif skt_sz == 2:
            family, sock_type = socket_type
            if sock_type not in [socket.SOCK_STREAM, socket.SOCK_DGRAM]:
                return False
        return True

    def register_new_interface(self, host, port, socket_type, data_semantics, server_mode=False,
                               hold_connection=False):

        if not self._is_valid_socket_type(socket_type):
            raise ValueError("Unrecognized socket type")

        self.multiple_destination = True
        self._host[data_semantics] = host
        self._port[data_semantics] = port
        self._socket_type[data_semantics] = socket_type
        self.known_semantics.append(data_semantics)
        self.server_mode[(host,port)] = server_mode
        self._default_fbk_id[(host, port)] = self._default_fbk_socket_id + ' - {:s}:{:d}'.format(host, port)
        self.hold_connection[(host, port)] = hold_connection

    def set_timeout(self, fbk_timeout, sending_delay):
        '''
        Set the time duration for feedback gathering and the sending delay above which
        we give up:
        - sending data to the target (client mode)
        - waiting for client connections before sending data to them (server mode)

        Args:
            fbk_timeout: time duration for feedback gathering (in seconds)
            sending_delay: sending delay (in seconds)
        '''
        assert sending_delay < fbk_timeout
        self._sending_delay = sending_delay
        self.set_feedback_timeout(fbk_timeout)

    def _set_feedback_timeout_specific(self, fbk_timeout):
        self._feedback_timeout = fbk_timeout
        if fbk_timeout == 0:
            # In this case, we do not alter 'sending_delay', as setting feedback timeout to 0
            # is a special case for retrieving residual feedback and because an alteration
            # of 'sending_delay' from this method is not recoverable.
            return

        if self._sending_delay > self._feedback_timeout:
            self._sending_delay = max(self._feedback_timeout-0.2, 0)

    def initialize(self):
        '''
        To be overloaded if some intial setup for the target is necessary. 
        '''
        return True

    def terminate(self):
        '''
        To be overloaded if some cleanup is necessary for stopping the target. 
        '''
        return True

    def add_additional_feedback_interface(self, host, port,
                                          socket_type=(socket.AF_INET, socket.SOCK_STREAM),
                                          fbk_id=None, fbk_length=None, server_mode=False):
        '''Allows to register additional socket to get feedback
        from. Connection is attempted be when target starts, that is
        when :meth:`NetworkTarget.start()` is called.
        '''
        self._default_additional_fbk_id += 1
        if fbk_id is None:
            fbk_id = 'Default Additional Feedback ID %d' % self._default_additional_fbk_id
        else:
            assert(not str(fbk_id).startswith('Default Additional Feedback ID'))
        self._additional_fbk_desc[fbk_id] = (host, port, socket_type, fbk_id, fbk_length, server_mode)
        self.hold_connection[(host, port)] = True

    def _custom_data_handling_before_emission(self, data_list):
        '''To be overloaded if you want to perform some operation before
        sending `data_list` to the target.

        Args:
          data_list (list): list of Data objects that will be sent to the target.
        '''
        pass

    def _feedback_handling(self, fbk, ref):
        '''To be overloaded if feedback from the target need to be filtered
        before being logged and/or collected in some way and/or for
        any other reasons.

        Args:
          fbk (bytes): feedback received by the target through a socket referenced by `ref`.
          ref (string): user-defined reference of the socket used to retrieve the feedback.

        Returns:
          tuple: a tuple `(new_fbk, status)` where `new_fbk` is the feedback
            you want to log and `status` is a status that enables you to notify a problem to the
            framework (should be positive if everything is fine, otherwise should be negative).
        '''
        return fbk, 0


    def listen_to(self, host, port, ref_id,
                  socket_type=(socket.AF_INET, socket.SOCK_STREAM),
                  chk_size=CHUNK_SZ, wait_time=None, hold_connection=True):
        '''
        Used for collecting feedback from the target while it is already started.
        '''
        self.hold_connection[(host, port)] = hold_connection
        self._raw_listen_to(host, port, ref_id, socket_type, chk_size, wait_time=wait_time)
        self._dynamic_interfaces[(host, port)] = (-1, ref_id)

    def _raw_listen_to(self, host, port, ref_id,
                       socket_type=(socket.AF_INET, socket.SOCK_STREAM),
                       chk_size=CHUNK_SZ, wait_time=None):

        if wait_time is None:
            wait_time = self._feedback_timeout

        initial_call = False
        if (host, port) not in self._server_sock2hp.values():
            initial_call = True

        connected_client_event = threading.Event()
        self._listen_to_target(host, port, socket_type,
                               self._handle_connection_to_fbk_server, args=(ref_id, chk_size, connected_client_event))

        if initial_call or not self.hold_connection[(host, port)]:
            connected_client_event.wait(wait_time)
            if not connected_client_event.is_set():
                self._logger.log_comment('WARNING: Feedback from ({:s}:{:d}) is not available as no client connects to us'.format(host, port))


    def connect_to(self, host, port, ref_id,
                   socket_type=(socket.AF_INET, socket.SOCK_STREAM),
                   chk_size=CHUNK_SZ, hold_connection=True):
        '''
        Used for collecting feedback from the target while it is already started.
        '''
        self.hold_connection[(host, port)] = hold_connection
        s = self._raw_connect_to(host, port, ref_id, socket_type, chk_size, hold_connection=hold_connection)
        self._dynamic_interfaces[(host, port)] = (s, ref_id)

        return s

    def _raw_connect_to(self, host, port, ref_id,
                        socket_type=(socket.AF_INET, socket.SOCK_STREAM),
                        chk_size=CHUNK_SZ, hold_connection=True):
        s = self._connect_to_target(host, port, socket_type)
        if s is None:
            self._logger.log_comment('WARNING: Unable to connect to {:s}:{:d}'.format(host, port))
            return None
        else:
            with self.socket_desc_lock:
                if s not in self._additional_fbk_sockets:
                    self._additional_fbk_sockets.append(s)
                    self._additional_fbk_ids[s] = ref_id
                    self._additional_fbk_lengths[s] = chk_size

        return s


    def remove_dynamic_interface(self, host, port):
        if (host, port) in self._dynamic_interfaces.keys():
            if (host, port) in self.hold_connection:
                del self.hold_connection[(host, port)]
                if (host, port) in self._hclient_hp2sock:
                    s = self._hclient_hp2sock[(host, port)]
                    del self._hclient_hp2sock[(host, port)]
                    del self._hclient_sock2hp[s]

            req_sock, ref_id = self._dynamic_interfaces[(host, port)]
            del self._dynamic_interfaces[(host, port)]
            with self.socket_desc_lock:
                if req_sock == -1:
                    for s, rid in copy.copy(self._additional_fbk_ids).items():
                        if ref_id == rid:
                            self._additional_fbk_sockets.remove(s)
                            del self._additional_fbk_ids[s]
                            del self._additional_fbk_lengths[s]

                elif req_sock in self._additional_fbk_sockets:
                    self._additional_fbk_sockets.remove(req_sock)
                    del self._additional_fbk_ids[req_sock]
                    del self._additional_fbk_lengths[req_sock]
            if req_sock != -1 and req_sock is not None:
                req_sock.close()
        else:
            print('\n*** WARNING: Unable to remove inexistent interface ({:s}:{:d})'.format(host,port))


    def remove_all_dynamic_interfaces(self):
        dyn_interface = copy.copy(self._dynamic_interfaces)
        for hp, req_sock in dyn_interface.items():
            self.remove_dynamic_interface(*hp)


    def _connect_to_additional_feedback_sockets(self):
        '''
        Connection to additional feedback sockets, if any.
        '''
        if self._additional_fbk_desc:
            for host, port, socket_type, fbk_id, fbk_length, server_mode in self._additional_fbk_desc.values():
                if server_mode:
                    self._raw_listen_to(host, port, fbk_id, socket_type, chk_size=fbk_length)
                else:
                    self._raw_connect_to(host, port, fbk_id, socket_type, chk_size=fbk_length)


    def _get_additional_feedback_sockets(self):
        '''Used if any additional socket to get feedback from has been added
        by :meth:`NetworkTarget.add_additional_feedback_interface()`,
        related to the data emitted if needed.

        Args:
          data (Data): the data that will be sent.

        Returns:
          tuple: list of sockets, dict of associated ids/names,
            dict of associated length (a length can be None)
        '''
        with self.socket_desc_lock:
            fbk_sockets = copy.copy(self._additional_fbk_sockets) if self._additional_fbk_sockets else None
            fbk_ids = copy.copy(self._additional_fbk_ids) if self._additional_fbk_sockets else None
            fbk_lengths = copy.copy(self._additional_fbk_lengths) if self._additional_fbk_sockets else None

        return fbk_sockets, fbk_ids, fbk_lengths


    def start(self):
        # Used by _raw_listen_to()
        self._server_sock2hp = {}
        self._server_thread_share = {}
        self._last_client_sock2hp = {}  # only for hold_connection
        self._last_client_hp2sock = {}  # only for hold_connection

        # Used by _raw_connect_to()
        self._hclient_sock2hp = {}  # only for hold_connection
        self._hclient_hp2sock = {}  # only for hold_connection

        self._additional_fbk_sockets = []
        self._additional_fbk_ids = {}
        self._additional_fbk_lengths = {}
        self._dynamic_interfaces = {}
        self._feedback_handled = None
        self.feedback_thread_qty = 0
        self.feedback_complete_cpt = 0
        self._sending_id = 0
        self._initial_sending_id = -1
        self._first_send_data_call = True
        self._thread_cpt = 0
        self._last_ack_date = None  # Note that `self._last_ack_date`
                                    # could be updated many times if
                                    # self.send_multiple_data() is
                                    # used.
        self._connect_to_additional_feedback_sockets()
        return self.initialize()

    def stop(self):
        self.stop_event.set()
        for s in self._server_sock2hp.keys():
            s.close()
        for s in self._last_client_sock2hp.keys():
            s.close()
        for s in self._hclient_sock2hp.keys():
            s.close()
        for s in self._additional_fbk_sockets:
            s.close()

        self._server_sock2hp = None
        self._server_thread_share = None
        self._last_client_sock2hp = None
        self._last_client_hp2sock = None
        self._hclient_sock2hp = None
        self._hclient_hp2sock = None
        self._additional_fbk_sockets = None
        self._additional_fbk_ids = None
        self._additional_fbk_lengths = None
        self._dynamic_interfaces = None

        return self.terminate()

    def send_data(self, data, from_fmk=False):
        self._before_sending_data(data, from_fmk)
        host, port, socket_type, server_mode = self._get_net_info_from(data)

        if data is None and (not self.hold_connection[(host, port)] or self.feedback_timeout == 0):
            # If data is None, it means that we want to collect feedback without sending data.
            # And that case makes sense only if we keep the socket (thus, 'hold_connection'
            # has to be True) or if a data callback wait for feedback (thus, in this case,
            # feedback_timeout will be > 0)
            return

        connected_client_event = None
        if server_mode:
            connected_client_event = threading.Event()
            self._listen_to_target(host, port, socket_type,
                                   self._handle_target_connection,
                                   args=(data, host, port, connected_client_event, from_fmk))
            connected_client_event.wait(self._sending_delay)
            if socket_type[1] == socket.SOCK_STREAM and not connected_client_event.is_set():
                self._feedback.set_error_code(-2)
                err_msg = ">>> WARNING: unable to send data because the target did not connect" \
                          " to us [{:s}:{:d}] <<<".format(host, port)
                # self._feedback.add_fbk_from(self._default_fbk_id[(host, port)], err_msg)
                self._feedback.add_fbk_from(self._INTERNALS_ID, err_msg)
        else:
            s = self._connect_to_target(host, port, socket_type)
            if s is None:
                self._feedback.set_error_code(-1)
                err_msg = '>>> WARNING: unable to send data to {:s}:{:d} <<<'.format(host, port)
                # self._feedback.add_fbk_from(self._default_fbk_id[(host, port)], err_msg)
                self._feedback.add_fbk_from(self._INTERNALS_ID, err_msg)
            else:
                self._send_data([s], {s:(data, host, port, None)}, self._sending_id, from_fmk)


    def send_multiple_data(self, data_list, from_fmk=False):
        self._before_sending_data(data_list, from_fmk)
        sockets = []
        data_refs = {}
        connected_client_event = {}
        client_event = None
        for data in data_list:
            host, port, socket_type, server_mode = self._get_net_info_from(data)
            if server_mode:
                connected_client_event[(host, port)] = threading.Event()
                self._listen_to_target(host, port, socket_type,
                                       self._handle_target_connection,
                                       args=(data, host, port,
                                             connected_client_event[(host, port)], from_fmk))
            else:
                s = self._connect_to_target(host, port, socket_type)
                if s is None:
                    self._feedback.set_error_code(-2)
                    err_msg = '>>> WARNING: unable to send data to {:s}:{:d} <<<'.format(host, port)
                    # self._feedback.add_fbk_from(self._default_fbk_id[(host, port)], err_msg)
                    self._feedback.add_fbk_from(self._INTERNALS_ID, err_msg)
                else:
                    if s not in sockets:
                        sockets.append(s)
                        data_refs[s] = (data, host, port, None)

        self._send_data(sockets, data_refs, self._sending_id, from_fmk)
        t0 = datetime.datetime.now()

        if connected_client_event:
            duration = 0
            client_event = connected_client_event
            client_event_copy = copy.copy(connected_client_event)
            while duration < self._sending_delay:
                if len(client_event) != len(client_event_copy):
                    client_event = copy.copy(client_event_copy)
                for ref, event in client_event.items():
                    event.wait(0.2)
                    if event.is_set():
                        del client_event_copy[ref]
                now = datetime.datetime.now()
                duration = (now - t0).total_seconds()

            for ref, event in connected_client_event.items():
                host, port = ref
                if not event.is_set():
                    self._feedback.set_error_code(-1)
                    err_msg = ">>> WARNING: unable to send data because the target did not connect" \
                              " to us [{:s}:{:d}] <<<".format(host, port)
                    # self._feedback.add_fbk_from(self._default_fbk_id[(host, port)], err_msg)
                    self._feedback.add_fbk_from(self._INTERNALS_ID, err_msg)

    def _get_data_semantic_key(self, data):
        if data is None:
            return self.UNKNOWN_SEMANTIC

        if data.node is None:
            if data.raw is None:
                print('\n*** ERROR: Empty data has been received!')
            return self.UNKNOWN_SEMANTIC

        semantics = data.node.get_semantics()
        if semantics is not None:
            matching_crit = semantics.what_match_from(self.known_semantics)
        else:
            matching_crit = None

        if matching_crit:
            key = matching_crit[0]
        else:
            key = self.UNKNOWN_SEMANTIC

        return key

    def _get_net_info_from(self, data):
        key = self._get_data_semantic_key(data)
        host = self._host[key]
        port = self._port[key]
        return host, port, self._socket_type[key], self.server_mode[(host, port)]

    def _connect_to_target(self, host, port, socket_type):
        if self.hold_connection[(host, port)] and (host, port) in self._hclient_hp2sock.keys():
            return self._hclient_hp2sock[(host, port)]

        skt_sz = len(socket_type)
        if skt_sz == 2:
            family, sock_type = socket_type
            proto = 0
        else:
            family, sock_type, proto = socket_type

        s = socket.socket(*socket_type)
        # s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)

        if sock_type == socket.SOCK_RAW:
            assert port == socket.ntohs(proto)
            try:
                s.bind((host, port))
            except socket.error as serr:
                print('\n*** ERROR(while binding socket): ' + str(serr))
                return False
        else:
            try:
                s.connect((host, port))
            except socket_error as serr:
                # if serr.errno != errno.ECONNREFUSED:
                print('\n*** ERROR(while connecting): ' + str(serr))
                return None

            s.setblocking(0)

        if self.hold_connection[(host, port)]:
            self._hclient_sock2hp[s] = (host, port)
            self._hclient_hp2sock[(host, port)] = s

        return s


    def _listen_to_target(self, host, port, socket_type, func, args=None):

        def start_raw_server(serversocket):
            server_thread = threading.Thread(None, self._raw_server_main, name='SRV-' + '',
                                             args=(serversocket, host, port, func))
            server_thread.start()

        skt_sz = len(socket_type)
        if skt_sz == 2:
            family, sock_type = socket_type
            proto = 0
        else:
            family, sock_type, proto = socket_type

        if (host, port) in self._server_sock2hp.values():
            # After data has been sent to the target that first
            # connect to us, new data is sent through the same socket
            # if hold_connection is set for this interface. And new
            # connection will always receive the most recent data to
            # send.
            if sock_type == socket.SOCK_DGRAM or sock_type == socket.SOCK_RAW:
                with self._server_thread_lock:
                    self._server_thread_share[(host, port)] = args
                if self.hold_connection[(host, port)] and (host, port) in self._last_client_hp2sock:
                    serversocket, _ = self._last_client_hp2sock[(host, port)]
                    start_raw_server(serversocket)
            else:
                with self._server_thread_lock:
                    self._server_thread_share[(host, port)] = args
                    if self.hold_connection[(host, port)] and (host, port) in self._last_client_hp2sock:
                        csocket, addr = self._last_client_hp2sock[(host, port)]
                    else:
                        csocket = None
                if csocket:
                    func(csocket, addr, args)
            return True

        serversocket = socket.socket(*socket_type)
        if sock_type != socket.SOCK_RAW:
            serversocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if sock_type == socket.SOCK_STREAM:
                serversocket.settimeout(0.2)

        if sock_type == socket.SOCK_DGRAM or sock_type == socket.SOCK_RAW:
            serversocket.settimeout(self._sending_delay)
            if sock_type == socket.SOCK_RAW:
                assert port == socket.ntohs(proto)

        try:
            serversocket.bind((host, port))
        except socket.error as serr:
            print('\n*** ERROR(while binding socket): ' + str(serr))
            return False

        self._server_sock2hp[serversocket] = (host, port)
        with self._server_thread_lock:
            self._server_thread_share[(host, port)] = args

        if sock_type == socket.SOCK_STREAM:
            serversocket.listen(5)
            server_thread = threading.Thread(None, self._server_main, name='SRV-' + '',
                                             args=(serversocket, host, port, func))
            server_thread.start()

        elif sock_type == socket.SOCK_DGRAM or sock_type == socket.SOCK_RAW:
            self._last_client_hp2sock[(host, port)] = (serversocket, None)
            self._last_client_sock2hp[serversocket] = (host, port)
            start_raw_server(serversocket)
        else:
            raise ValueError("Unrecognized socket type")

    def _server_main(self, serversocket, host, port, func):
        while not self.stop_event.is_set():
            try:
                # accept connections from outside
                (clientsocket, address) = serversocket.accept()
            except socket.timeout:
                pass
            except OSError as e:
                if e.errno == 9: # [Errno 9] Bad file descriptor
                    # TOFIX: It may occur with python3.
                    # In this case the resource seem to have been released by
                    # the OS whereas there is still a reference on it.
                    pass
                else:
                    raise
            else:
                with self._server_thread_lock:
                    args = self._server_thread_share[(host, port)]
                func(clientsocket, address, args)

    def _raw_server_main(self, serversocket, host, port, func):

        with self._server_thread_lock:
            args = self._server_thread_share[(host, port)]

        retry = 0
        while retry < 10:
            try:
                # accept UDP from outside
                if args[0] is not None:
                    data, address = serversocket.recvfrom(self.CHUNK_SZ)
                else:
                    data, address = None, None
            except socket.timeout:
                break
            except OSError as e:
                if e.errno == 9: # [Errno 9] Bad file descriptor
                    break
                elif e.errno == 11: # [Errno 11] Resource temporarily unavailable
                    retry += 1
                    time.sleep(0.5)
                    continue
                else:
                    raise
            except socket.error as serr:
                if serr.errno == 11:  # [Errno 11] Resource temporarily unavailable
                    retry += 1
                    time.sleep(0.5)
                    continue
            else:
                serversocket.settimeout(self.feedback_timeout)
                func(serversocket, address, args, pre_fbk=data)
                break

    def _handle_connection_to_fbk_server(self, clientsocket, address, args, pre_fbk=None):
        fbk_id, fbk_length, connected_client_event = args
        connected_client_event.set()
        with self.socket_desc_lock:
            self._additional_fbk_sockets.append(clientsocket)
            self._additional_fbk_ids[clientsocket] = fbk_id
            self._additional_fbk_lengths[clientsocket] = fbk_length

    def _handle_target_connection(self, clientsocket, address, args, pre_fbk=None):
        data, host, port, connected_client_event, from_fmk = args
        if self.hold_connection[(host, port)]:
            with self._server_thread_lock:
                self._last_client_hp2sock[(host, port)] = (clientsocket, address)
                self._last_client_sock2hp[clientsocket] = (host, port)
        connected_client_event.set()
        self._send_data([clientsocket], {clientsocket:(data, host, port, address)}, self._sending_id,
                        from_fmk=from_fmk, pre_fbk={clientsocket: pre_fbk})


    def _collect_feedback_from(self, fbk_sockets, fbk_ids, fbk_lengths, epobj, fileno2fd,
                               send_id, fbk_timeout, from_fmk, pre_fbk):

        def _check_and_handle_obsolete_socket(skt, error=None, error_list=None):
            # print('\n*** NOTE: Remove obsolete socket {!r}'.format(socket))
            try:
                epobj.unregister(skt)
            except ValueError as e:
                # in python3, file descriptor == -1 witnessed (!?)
                print('\n*** ERROR(check obsolete socket): ' + str(e))
            except socket.error as serr:
                # in python2, bad file descriptor (errno 9) witnessed
                print('\n*** ERROR(check obsolete socket): ' + str(serr))

            self._server_thread_lock.acquire()
            if socket in self._last_client_sock2hp.keys():
                if error is not None:
                    error_list.append((fbk_ids[socket], error))
                host, port = self._last_client_sock2hp[socket]
                del self._last_client_sock2hp[socket]
                del self._last_client_hp2sock[(host, port)]
                self._server_thread_lock.release()
            else:
                self._server_thread_lock.release()
                with self.socket_desc_lock:
                    if socket in self._hclient_sock2hp.keys():
                        if error is not None:
                            error_list.append((fbk_ids[socket], error))
                        host, port = self._hclient_sock2hp[socket]
                        del self._hclient_sock2hp[socket]
                        del self._hclient_hp2sock[(host, port)]
                    if socket in self._additional_fbk_sockets:
                        if error is not None:
                            error_list.append((self._additional_fbk_ids[socket], error))
                        self._additional_fbk_sockets.remove(socket)
                        del self._additional_fbk_ids[socket]
                        del self._additional_fbk_lengths[socket]

        chunks = collections.OrderedDict()
        t0 = datetime.datetime.now()
        duration = 0
        first_pass = True
        ack_date = None
        dont_stop = True

        bytes_recd = {}
        for fd in fbk_sockets:
            bytes_recd[fd] = 0
            chunks[fd] = []
            if pre_fbk is not None and fd in pre_fbk and pre_fbk[fd] is not None:
                chunks[fd].append(pre_fbk[fd])

        socket_errors = []

        while dont_stop:
            ready_to_read = []
            for fd, ev in epobj.poll(timeout=0.2):
                skt = fileno2fd[fd]
                if ev != select.EPOLLIN:
                    _check_and_handle_obsolete_socket(skt, error=ev, error_list=socket_errors)
                    if skt in fbk_sockets:
                        fbk_sockets.remove(skt)
                    continue
                ready_to_read.append(skt)

            now = datetime.datetime.now()
            duration = (now - t0).total_seconds()
            if ready_to_read:
                if first_pass:
                    first_pass = False
                    self._register_last_ack_date(now)
                for s in ready_to_read:
                    if fbk_lengths[s] is None:
                        sz = NetworkTarget.CHUNK_SZ
                    else:
                        sz = min(fbk_lengths[s] - bytes_recd[s], NetworkTarget.CHUNK_SZ)

                    retry = 0
                    socket_timed_out = False
                    while retry < 3:
                        try:
                            chunk = s.recv(sz)
                        except socket.timeout:
                            chunk = b''
                            socket_timed_out = True  # UDP
                            break
                        except socket.error as serr:
                            chunk = b''
                            print('\n*** ERROR[{!s}] (while receiving): {:s}'.format(
                                serr.errno, str(serr)))
                            if serr.errno == socket.errno.EAGAIN:
                                retry += 1
                                time.sleep(2)
                                continue
                            else:
                                break
                        else:
                            break

                    if chunk == b'':
                        print('\n*** NOTE: Nothing more to receive from: {!r}'.format(fbk_ids[s]))
                        fbk_sockets.remove(s)
                        _check_and_handle_obsolete_socket(s)
                        if not socket_timed_out:
                            s.close()
                        continue
                    else:
                        bytes_recd[s] = bytes_recd[s] + len(chunk)
                        chunks[s].append(chunk)

            if fbk_sockets:
                for s in fbk_sockets:
                    if s in ready_to_read:
                        s_fbk_len = fbk_lengths[s]
                        if s_fbk_len is None or bytes_recd[s] < s_fbk_len:
                            dont_stop = True
                            break
                    else:
                        dont_stop = True
                        break
                else:
                    dont_stop = False

                if duration > fbk_timeout:
                    dont_stop = False

            else:
                dont_stop = False

        for s, chks in chunks.items():
            fbk = b'\n'.join(chks)
            with self._fbk_handling_lock:
                fbkid = fbk_ids[s]
                fbk, err = self._feedback_handling(fbk, fbkid)
                self._feedback_collect(fbk, fbkid, error=err)
                if (self._additional_fbk_sockets is None or s not in self._additional_fbk_sockets) and \
                        (self._hclient_sock2hp is None or s not in self._hclient_sock2hp.keys()) and \
                        (self._last_client_sock2hp is None or s not in self._last_client_sock2hp.keys()):
                    s.close()

        with self._fbk_handling_lock:
            for fbkid, ev in socket_errors:
                self._feedback_collect(">>> ERROR[{:d}]: unable to interact with '{:s}' "
                                       "<<<".format(ev,fbkid), fbkid, error=-ev)
            if from_fmk:
                self._feedback_complete(send_id)

        return


    def _send_data(self, sockets, data_refs, sid, from_fmk, pre_fbk=None):
        if sid != self._initial_sending_id:
            self._initial_sending_id = sid
            # self._first_send_data_call = True

        epobj = select.epoll()
        fileno2fd = {}

        if self._first_send_data_call:
            self._first_send_data_call = False

            fbk_sockets, fbk_ids, fbk_lengths = self._get_additional_feedback_sockets()
            if fbk_sockets:
                for fd in fbk_sockets:
                    epobj.register(fd, select.EPOLLIN)
                    fileno2fd[fd.fileno()] = fd
        else:
            fbk_sockets, fbk_ids, fbk_lengths = None, None, None

        if data_refs[sockets[0]][0] is None:
            # We check the data to send. If it is None, we only collect feedback from the sockets.
            # This is used by self.collect_feedback_without_sending()
            if fbk_sockets is None:
                assert fbk_ids is None
                assert fbk_lengths is None
                fbk_sockets = []
                fbk_ids = {}
                fbk_lengths = {}

            for s in sockets:
                data, host, port, address = data_refs[s]
                epobj.register(s, select.EPOLLIN)
                fileno2fd[s.fileno()] = s
                fbk_sockets.append(s)
                fbk_ids[s] = self._default_fbk_id[(host, port)]
                fbk_lengths[s] = self.feedback_length

            self._start_fbk_collector(fbk_sockets, fbk_ids, fbk_lengths, epobj, fileno2fd, from_fmk)

            return

        ready_to_read, ready_to_write, in_error = select.select([], sockets, [], self._sending_delay)
        if ready_to_write:

            for s in ready_to_write:
                add_main_socket = True
                data, host, port, address = data_refs[s]
                epobj.register(s, select.EPOLLIN)
                fileno2fd[s.fileno()] = s

                raw_data = data.to_bytes()
                totalsent = 0
                send_retry = 0
                while totalsent < len(raw_data) and send_retry < 10:
                    try:
                        if address is None:
                            sent = s.send(raw_data[totalsent:])
                        else:
                            # with SOCK_RAW, address is ignored
                            sent = s.sendto(raw_data[totalsent:], address)
                    except socket.error as serr:
                        send_retry += 1
                        print('\n*** ERROR(while sending): ' + str(serr))
                        if serr.errno == socket.errno.EWOULDBLOCK:
                            time.sleep(0.2)
                            continue
                        elif serr.errno == socket.errno.EMSGSIZE:  # for SOCK_RAW
                            self._feedback.add_fbk_from(self._INTERNALS_ID, 'Message was not sent because it was too long!')
                            break
                        else:
                            # add_main_socket = False
                            raise TargetStuck("system not ready for sending data! {!r}".format(serr))
                    else:
                        if sent == 0:
                            s.close()
                            raise TargetStuck("socket connection broken")
                        totalsent = totalsent + sent

                if fbk_sockets is None:
                    assert fbk_ids is None
                    assert fbk_lengths is None
                    fbk_sockets = []
                    fbk_ids = {}
                    fbk_lengths = {}
                # else:
                #     assert(self._default_fbk_id[(host, port)] not in fbk_ids.values())

                if add_main_socket:
                    fbk_sockets.append(s)
                    fbk_ids[s] = self._default_fbk_id[(host, port)]
                    fbk_lengths[s] = self.feedback_length


            self._start_fbk_collector(fbk_sockets, fbk_ids, fbk_lengths, epobj, fileno2fd, from_fmk,
                                      pre_fbk=pre_fbk)

        else:
            raise TargetStuck("system not ready for sending data!")


    def _start_fbk_collector(self, fbk_sockets, fbk_ids, fbk_lengths, epobj, fileno2fd, from_fmk,
                             pre_fbk=None):
        self._thread_cpt += 1
        if from_fmk:
            self.feedback_thread_qty += 1
        feedback_thread = threading.Thread(None, self._collect_feedback_from,
                                           name='FBK-' + repr(self._sending_id) + '#' + repr(self._thread_cpt),
                                           args=(fbk_sockets, fbk_ids, fbk_lengths, epobj, fileno2fd,
                                                 self._sending_id, self._feedback_timeout, from_fmk,
                                                 pre_fbk))
        feedback_thread.start()


    def _feedback_collect(self, fbk, ref, error=0):
        if error < 0:
            self._feedback.set_error_code(error)
        self._feedback.add_fbk_from(ref, fbk)

    def _feedback_complete(self, sid):
        if sid == self._sending_id:
            self.feedback_complete_cpt += 1
        if self.feedback_complete_cpt == self.feedback_thread_qty:
            self._feedback_handled = True

    def _before_sending_data(self, data_list, from_fmk):
        if from_fmk:
            self._last_ack_date = None
            self._first_send_data_call = True  # related to additional feedback
            self._feedback_handled = False
            self._sending_id += 1
        else:
            self._first_send_data_call = False  # we ignore all additional feedback

        if data_list is None:
            return

        if isinstance(data_list, Data):
            data_list = [data_list]

        for data in data_list:
            if data.node is None:
                continue
            _, _, socket_type, _ = self._get_net_info_from(data)
            if socket_type[1] == socket.SOCK_RAW:
                data.node.freeze()
                try:
                    data.node[self._mac_src_semantic] = self._mac_src
                except ValueError:
                    self._logger.log_comment('WARNING: Unable to set the MAC SOURCE on the packet')
                if self._mac_dst is not None:
                    try:
                        data.node[self._mac_dst_semantic] = self._mac_dst
                    except ValueError:
                        self._logger.log_comment('WARNING: Unable to set the MAC DESTINATION on the packet')

        self._custom_data_handling_before_emission(data_list)


    def collect_feedback_without_sending(self):
        self.send_data(None, from_fmk=True)
        return True

    def get_feedback(self):
        return self._feedback

    def is_target_ready_for_new_data(self):
        # We answer we are ready if at least one receiver has
        # terminated its job, either because the target answered to
        # it, or because of the current specified timeout.
        if self._feedback_handled:
            return True
        else:
            return False

    def _register_last_ack_date(self, ack_date):
        self._last_ack_date = ack_date

    def get_last_target_ack_date(self):
        return self._last_ack_date

    def _get_socket_type(self, host, port):
        for key, h in self._host.items():
            if h == host and self._port[key] == port:
                st = self._socket_type[key]
                if st[:2] == (socket.AF_INET, socket.SOCK_STREAM):
                    return 'STREAM'
                elif st[:2] == (socket.AF_INET, socket.SOCK_DGRAM):
                    return 'DGRAM'
                elif st[:2] == (socket.AF_PACKET, socket.SOCK_RAW):
                    return 'RAW'
                else:
                    return repr(st)
        else:
            return None

    def get_description(self):
        desc_added = []
        desc = ''
        for key, host in self._host.items():
            port = self._port[key]
            if (host, port) in desc_added:
                continue
            desc_added.append((host, port))
            server_mode = self.server_mode[(host, port)]
            hold_connection = self.hold_connection[(host, port)]
            socket_type = self._get_socket_type(host, port)
            desc += '{:s}:{:d}#{!s} (serv:{!r},hold:{!r}), '.format(
                host, port, socket_type, server_mode, hold_connection)

        return desc[:-2]



class PrinterTarget(Target):

    def __init__(self, tmpfile_ext):
        self.__suffix = '{:0>12d}'.format(random.randint(2**16, 2**32))
        self.__feedback = TargetFeedback()
        self.__target_ip = None
        self.__target_port = None
        self.__printer_name = None
        self.__cpt = None
        self.set_tmp_file_extension(tmpfile_ext)

    def set_tmp_file_extension(self, tmpfile_ext):
        self._tmpfile_ext = tmpfile_ext

    def set_target_ip(self, target_ip):
        self.__target_ip = target_ip

    def get_target_ip(self):
        return self.__target_ip

    def set_target_port(self, target_port):
        self.__target_port = target_port

    def get_target_port(self):
        return self.__target_port

    def set_printer_name(self, printer_name):
        self.__printer_name = printer_name

    def get_printer_name(self):
        return self.__printer_name

    def start(self):
        self.__cpt = 0

        if not cups_module:
            print('/!\\ ERROR /!\\: the PrinterTarget has been disabled because python-cups module is not installed')
            return False

        if not self.__target_ip:
            print('/!\\ ERROR /!\\: the PrinterTarget IP has not been set')
            return False

        if self.__target_port is None:
            self.__target_port = 631

        cups.setServer(self.__target_ip)
        cups.setPort(self.__target_port)

        self.__connection = cups.Connection()

        try:
            printers = self.__connection.getPrinters()
        except cups.IPPError as err:
            print('CUPS Server Errror: ', err)
            return False
        
        if self.__printer_name is not None:
            try:
                params = printers[self.__printer_name]
            except:
                print("Printer '%s' is not connected to CUPS server!" % self.__printer_name)
                return False
        else:
            self.__printer_name, params = printers.popitem()

        print("\nDevice-URI: %s\nPrinter Name: %s" % (params["device-uri"], self.__printer_name))

        return True

    def send_data(self, data, from_fmk=False):

        data = data.to_bytes()
        wkspace = workspace_folder
        file_name = os.path.join(wkspace, 'fuzz_test_' + self.__suffix + self._tmpfile_ext)

        with open(file_name, 'wb') as f:
             f.write(data)

        inc = '_{:0>5d}'.format(self.__cpt)
        self.__cpt += 1

        try:
            self.__connection.printFile(self.__printer_name, file_name, 'job_'+ self.__suffix + inc, {})
        except cups.IPPError as err:
            print('CUPS Server Errror: ', err)


class LocalTarget(Target):

    def __init__(self, tmpfile_ext, target_path=None):
        self.__suffix = '{:0>12d}'.format(random.randint(2**16, 2**32))
        self.__app = None
        self.__pre_args = None
        self.__post_args = None
        self._data_sent = None
        self._feedback_computed = None
        self.__feedback = TargetFeedback()
        self.set_target_path(target_path)
        self.set_tmp_file_extension(tmpfile_ext)

    def set_tmp_file_extension(self, tmpfile_ext):
        self._tmpfile_ext = tmpfile_ext

    def set_target_path(self, target_path):
        self.__target_path = target_path

    def get_target_path(self):
        return self.__target_path

    def set_pre_args(self, pre_args):
        self.__pre_args = pre_args

    def get_pre_args(self):
        return self.__pre_args

    def set_post_args(self, post_args):
        self.__post_args = post_args

    def get_post_args(self):
        return self.__post_args

    def initialize(self):
        '''
        To be overloaded if some intial setup for the target is necessary.
        '''
        return True

    def terminate(self):
        '''
        To be overloaded if some cleanup is necessary for stopping the target.
        '''
        return True

    def start(self):
        if not self.__target_path:
            print('/!\\ ERROR /!\\: the LocalTarget path has not been set')
            return False

        self._data_sent = False

        return self.initialize()

    def stop(self):
        return self.terminate()

    def _before_sending_data(self):
        self._feedback_computed = False

    def send_data(self, data, from_fmk=False):
        self._before_sending_data()
        data = data.to_bytes()
        wkspace = workspace_folder

        name = os.path.join(wkspace, 'fuzz_test_' + self.__suffix + self._tmpfile_ext)
        with open(name, 'wb') as f:
             f.write(data)

        if self.__pre_args is not None and self.__post_args is not None:
            cmd = [self.__target_path] + self.__pre_args.split() + [name] + self.__post_args.split()
        elif self.__pre_args is not None:
            cmd = [self.__target_path] + self.__pre_args.split() + [name]
        elif self.__post_args is not None:
            cmd = [self.__target_path, name] + self.__post_args.split()
        else:
            cmd = [self.__target_path, name]

        self.__app = subprocess.Popen(args=cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        fl = fcntl.fcntl(self.__app.stderr, fcntl.F_GETFL)
        fcntl.fcntl(self.__app.stderr, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        fl = fcntl.fcntl(self.__app.stdout, fcntl.F_GETFL)
        fcntl.fcntl(self.__app.stdout, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        self._data_sent = True
        
    def cleanup(self):
        if self.__app is None:
            return

        try:
            os.kill(self.__app.pid, signal.SIGTERM)
        except:
            print("\n*** WARNING: cannot kill application with PID {:d}".format(self.__app.pid))
        finally:
            self._data_sent = False

    def get_feedback(self, delay=0.2):
        if self._feedback_computed:
            return self.__feedback
        else:
            self._feedback_computed = True

        if self.__app is None and self._data_sent:
            self.__feedback.set_error_code(-3)
            self.__feedback.add_fbk_from("LocalTarget", "Application has terminated (crash?)")
            return self.__feedback
        elif self.__app is None:
            return self.__feedback

        exit_status = self.__app.poll()
        if exit_status is not None and exit_status < 0:
            self.__feedback.set_error_code(exit_status)
            self.__feedback.add_fbk_from("Application[{:d}]".format(self.__app.pid),
                                         "Negative return status ({:d})".format(exit_status))

        err_detected = False
        ret = select.select([self.__app.stdout, self.__app.stderr], [], [], delay)
        if ret[0]:
            byte_string = b''
            for fd in ret[0][:-1]:
                byte_string += fd.read() + b'\n\n'

            if b'error' in byte_string or b'invalid' in byte_string:
                self.__feedback.set_error_code(-1)
                self.__feedback.add_fbk_from("LocalTarget[stdout]", "Application outputs errors on stdout")

            stderr_msg = ret[0][-1].read()
            if stderr_msg:
                self.__feedback.set_error_code(-2)
                self.__feedback.add_fbk_from("LocalTarget[stderr]", "Application outputs on stderr")
                byte_string += stderr_msg
            else:
                byte_string = byte_string[:-2]  # remove '\n\n'

        else:
            byte_string = b''

        self.__feedback.set_bytes(byte_string)

        return self.__feedback


class SIMTarget(Target):
    delay_between_write = 0.1  # without, it seems some commands can be lost

    def __init__(self, serial_port, baudrate, pin_code, targeted_tel_num):
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.tel_num = targeted_tel_num
        self.pin_code = pin_code
        if sys.version_info[0]>2:
            self.pin_code = bytes(self.pin_code, 'latin_1')
        self.set_feedback_timeout(2)

    def start(self):

        if not serial_module:
            print('/!\\ ERROR /!\\: the PhoneTarget has been disabled because '
                  'python-serial module is not installed')
            return False

        self.ser = serial.Serial(self.serial_port, self.baudrate, timeout=2,
                                 dsrdtr=True, rtscts=True)

        self.ser.write(b"ATE1\r\n") # echo ON
        time.sleep(self.delay_between_write)
        self.ser.write(b"AT+CMEE=1\r\n") # enable extended error reports
        time.sleep(self.delay_between_write)
        self.ser.write(b"AT+CPIN?\r\n") # need to unlock?
        cpin_fbk = self._retrieve_feedback_from_serial(timeout=0)
        if cpin_fbk.find(b'SIM PIN') != -1:
            # Note that if SIM is already unlocked modem will answer CME ERROR: 3
            # if we try to unlock it again.
            # So we need to unlock only when it is needed.
            # If modem is unlocked the answer will be: CPIN: READY
            # otherwise it will be: CPIN: SIM PIN.
            self.ser.write(b"AT+CPIN="+self.pin_code+b"\r\n") # enter pin code
        time.sleep(self.delay_between_write)
        self.ser.write(b"AT+CMGF=0\r\n") # PDU mode
        time.sleep(self.delay_between_write)
        self.ser.write(b"AT+CSMS=0\r\n") # check if modem can process SMS
        time.sleep(self.delay_between_write)

        fbk = self._retrieve_feedback_from_serial(timeout=1)
        code = 0 if fbk.find(b'ERROR') == -1 else -1
        self._logger.collect_target_feedback(fbk, status_code=code)
        if code < 0:
            self._logger.print_console(cpin_fbk+fbk, rgb=Color.ERROR)

        return False if code < 0 else True

    def stop(self):
        self.ser.close()

    def _retrieve_feedback_from_serial(self, timeout=None):
        feedback = b''
        t0 = datetime.datetime.now()
        duration = -1
        timeout = self.feedback_timeout if timeout is None else timeout
        while duration < timeout:
            now = datetime.datetime.now()
            duration = (now - t0).total_seconds()
            time.sleep(0.1)
            fbk = self.ser.readline()
            if fbk.strip():
                feedback += fbk

        return feedback

    def send_data(self, data, from_fmk=False):
        node_list = data.node[NodeSemanticsCriteria(mandatory_criteria=['tel num'])]
        if node_list and len(node_list)==1:
            node_list[0].set_values(value_type=GSMPhoneNum(val_list=[self.tel_num]))
        else:
            print('\nWARNING: Data does not contain a mobile number.')
        pdu = b''
        raw_data = data.to_bytes()
        for c in raw_data:
            if sys.version_info[0] == 2:
                c = ord(c)
            pdu += binascii.b2a_hex(struct.pack('B', c))
        pdu = pdu.upper()

        pdu = b'00' + pdu + b"\x1a\r\n"

        self.ser.write(b"AT+CMGS=23\r\n") # PDU mode
        time.sleep(self.delay_between_write)
        self.ser.write(pdu)

        fbk = self._retrieve_feedback_from_serial()
        code = 0 if fbk.find(b'ERROR') == -1 else -1
        self._logger.collect_target_feedback(fbk, status_code=code)

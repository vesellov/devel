
"""
..module:: file_queue
"""

import os
import time
import struct
import cStringIO
import random 

from twisted.internet import reactor

from logs import lg

from lib import udp
from lib import tmpfile
from lib import settings
from lib import misc

import udp_interface
import udp_session
import udp_stream

#------------------------------------------------------------------------------ 

MAX_SIMULTANEOUS_STREAMS = 4

#------------------------------------------------------------------------------ 

class FileQueue:
    def __init__(self, session):
        self.session = session
        self.streams = {}
        self.outboxFiles = {}
        self.inboxFiles = {}
        self.outboxQueue = []
        self.dead_streams = set()
        
    def make_unique_stream_id(self):
        return int(str(random.randint(100,999))+str(int(time.time() * 100.0))[7:])
        
    def close(self):
        for stream in self.streams.values():
            stream.close()
        self.streams.clear()
        self.outboxFiles.clear()
        self.inboxFiles.clear()
        for filename, description, result_defer, single in self.outboxQueue:
            self.on_failed_outbox_queue_item(filename, description, 'session was closed', result_defer, single)
        self.outboxQueue = []

    def do_send_data(self, stream_id, outfile, output):
        newoutput = ''.join((
            struct.pack('i', stream_id),
            struct.pack('i', outfile.size),
            output))
        # lg.out(18, '<-out DATA %d %s' % (stream_id, struct.unpack('i', output[:4])[0]))
        return self.session.send_packet(udp.CMD_DATA, newoutput)
    
    def do_send_ack(self, stream_id, infile, ack_data):
        newoutput = ''.join((
            struct.pack('i', stream_id),
            ack_data))
#        if ack_data:
#            lg.out(18, '<-out ACK %d %s' % (stream_id,
#                ','.join(map(lambda x: str(struct.unpack('i', x)[0]), [ack_data[i:i+4] for i in range(0, len(ack_data), 4)]))))
#        else:
#
#            lg.out(18, '<-out ACK %d ZERO' % stream_id)
        return self.session.send_packet(udp.CMD_ACK, newoutput)

    def append_outbox_file(self, filename, description='', result_defer=None, single=False):
        self.outboxQueue.append((filename, description, result_defer, single))
        udp_session.process_sessions()
        
    def insert_outbox_file(self, filename, description='', result_defer=None, single=False):
        self.outboxQueue.insert(0, (filename, description, result_defer, single))
        udp_session.process_sessions()
        
    def process_outbox_queue(self):
        has_reads = False
        while len(self.outboxQueue) > 0 and len(self.streams) < MAX_SIMULTANEOUS_STREAMS:        
            filename, description, result_defer, single = self.outboxQueue.pop(0)
            has_reads = True
            # we have a queue of files to be sent
            # somehow file may be removed before we start sending it
            # so I check it here and skip not existed files
            if not os.path.isfile(filename):
                self.on_failed_outbox_queue_item(filename, description, 'file not exist', result_defer, single)
                continue
            try:
                filesize = os.path.getsize(filename)
            except:
                self.on_failed_outbox_queue_item(filename, description, 'can not get file size', result_defer, single)
                continue
            self.start_outbox_file(filename, filesize, description, result_defer, single)
        return has_reads
    
    def process_outbox_files(self):
        has_sends = False
        for outfile in self.outboxFiles.values():
            has_sends = has_sends or outfile.process()
        #for stream in self.streams.values():
        #    stream.process()
        return has_sends

    def start_outbox_file(self, filename, filesize, description, result_defer, single):
        stream_id = self.make_unique_stream_id()
        lg.out(18, 'udp_file_queue.start_outbox_file %d %s %s %d %s' % (
            stream_id, description, os.path.basename(filename), filesize, self.session.peer_id))
        outfile = OutboxFile(self, stream_id, filename, filesize, description, result_defer, single)
        outstream = udp_stream.UDPStream(stream_id, outfile, self)
        self.streams[stream_id] = outstream
        self.outboxFiles[stream_id] = outfile
        if not single:
            d = udp_interface.interface_register_file_sending(
                self.session.peer_id, self.session.peer_idurl, filename, description)
            d.addCallback(self.on_outbox_file_registered, stream_id)
            d.addErrback(self.on_outbox_file_register_failed, stream_id)
            self.outboxFiles[stream_id].registration = d
        
    def start_inbox_file(self, stream_id, data_size):
        lg.out(18, 'udp_file_queue.start_inbox_file %d %d %s' % (stream_id, data_size, self.session.peer_id))
        infile = InboxFile(self, stream_id, data_size)
        instream = udp_stream.UDPStream(stream_id, infile, self)
        self.streams[stream_id] = instream
        self.inboxFiles[stream_id] = infile
        d = udp_interface.interface_register_file_receiving(
            self.session.peer_id, self.session.peer_idurl, infile.filename, infile.size)
        d.addCallback(self.on_inbox_file_registered, stream_id)
        d.addErrback(self.on_inbox_file_register_failed, stream_id)
        self.inboxFiles[stream_id].registration = d

    def close_stream(self, stream_id):
        s = self.streams.pop(stream_id)
        s.close()
        self.dead_streams.add(stream_id)
        
    def close_outbox_file(self, stream_id):
        self.outboxFiles[stream_id].close()
        del self.outboxFiles[stream_id]

    def close_inbox_file(self, stream_id):
        self.inboxFiles[stream_id].close()   
        del self.inboxFiles[stream_id]   

    def report_outbox_file(self, outfile, status, error_message=''):    
        lg.out(18, 'udp_file_queue.report_outbox_file %s %s %d bytes "%s"' % (
            outfile.transfer_id, status, outfile.bytes_delivered, error_message))
        udp_interface.interface_unregister_file_sending(
            outfile.transfer_id, status, outfile.bytes_delivered, error_message)

    def report_inbox_file(self, infile, status, error_message=''):
        lg.out(18, 'udp_file_queue.report_inbox_file {%s} %s %s %d bytes "%s"' % (
            os.path.basename(infile.filename), infile.transfer_id, 
            status, infile.bytes_received, error_message))
        udp_interface.interface_unregister_file_receiving(
            infile.transfer_id, status, infile.bytes_received, error_message)

    #------------------------------------------------------------------------------ 

    def on_received_data_packet(self, payload):
        inp = cStringIO.StringIO(payload)
        try:
            stream_id = struct.unpack('i', inp.read(4))[0]
            data_size = struct.unpack('i', inp.read(4))[0]
        except:
            inp.close()
            lg.exc()
            return
        if not self.session.peer_id:
            inp.close()
            lg.warn('peer id is unknown yet %s' % stream_id)
            self.do_send_ack(stream_id, None, '')
            return
        if stream_id not in self.streams.keys():
            if stream_id in self.dead_streams:
                inp.close()
                self.do_send_ack(stream_id, None, '')
                lg.warn('old block %s' % stream_id)
                return
            if len(self.streams) >= MAX_SIMULTANEOUS_STREAMS:
                # too many incoming streams, seems remote side is cheating - drop that session!
                # TODO : need to add some protection - keep a list of bad guys?  
                inp.close()
                # lg.warn('too many incoming files for session %s' % str(self.session))
                # self.session.automat('shutdown')
                lg.warn('too many incoming files %s %s' % (stream_id, self.session.peer_id))
                self.do_send_ack(stream_id, None, '') 
                return
            self.start_inbox_file(stream_id, data_size)
        try:
            self.streams[stream_id].block_received(inp)
        except:
            lg.exc()
        inp.close()
        
    def on_received_ack_packet(self, payload):
        inp = cStringIO.StringIO(payload)
        try:
            stream_id = struct.unpack('i', inp.read(4))[0]
        except:
            inp.close()
            lg.exc()
            # self.session.automat('shutdown') 
            return
        if stream_id not in self.streams.keys():
            inp.close()
            # if not self.receivedFiles.has_key(stream_id):
            # lg.warn('unknown stream_id=%d in ACK packet from %s' % (
            #     stream_id, self.session.peer_address))
            # self.session.automat('shutdown')
            if stream_id in self.dead_streams:
                # print 'old ack', stream_id
                pass
            else:
                lg.warn('%s - what a stream ???' % stream_id) 
            # self.session.automat('shutdown')
            return
        try:
            self.streams[stream_id].ack_received(inp)
        except:
            lg.exc()
            self.session.automat('shutdown') 
        inp.close()

    def on_inbox_file_done(self, infile, status, error_message=None):
        # print 'on_inbox_file_done',
        # print infile.stream_id, infile.registration, 
        # print infile.transfer_id, infile.filename, 
        stream_id = infile.stream_id
        if infile.registration:
            # print '... registration in process'
            return
        if infile.transfer_id:
            self.report_inbox_file(infile, status, error_message)
        self.close_stream(stream_id)
        self.close_inbox_file(stream_id)
        # else:
        #     lg.warn('transfer_id is None, stream_id=%d' % stream_id)
        # self.receivedFiles[stream_id] = time.time()
        # self.erase_old_stream_ids()
        # print ' - closed'

    def on_outbox_file_done(self, outfile, status, error_message=''):
        stream_id = outfile.stream_id
        lg.out(18, 'udp_file_queue.on_outbox_file_done %s (%d bytes) %s "%s"' % (
            stream_id, outfile.size, status, error_message))
        if outfile.result_defer:
            outfile.result_defer.callback((outfile, status, error_message))
            outfile.result_defer = None
        if outfile.registration:
            return
        if outfile.transfer_id:
            self.report_outbox_file(outfile, 'finished')
        self.close_stream(stream_id)
        self.close_outbox_file(stream_id)
           
    def on_inbox_file_registered(self, response, stream_id):
        try:
            transfer_id = int(response)
        except:
            transfer_id = None
        # print 'on_inbox_file_registered', stream_id, transfer_id, self.inboxFiles[stream_id].filename
        # assert stream_id in self.inboxFiles.keys()
        self.inboxFiles[stream_id].transfer_id = transfer_id
        self.inboxFiles[stream_id].registration = None
        if self.inboxFiles[stream_id].is_done():
            self.report_inbox_file(self.inboxFiles[stream_id], 'finished')
            self.close_stream(stream_id)
            self.close_inbox_file(stream_id)
            # self.receivedFiles[stream_id] = time.time()
            # self.erase_old_stream_ids()
        elif self.inboxFiles[stream_id].is_timed_out():
            self.report_inbox_file(self.inboxFiles[stream_id], 'failed', 'timeout receiving')
            self.close_stream(stream_id)
            self.close_inbox_file(stream_id)
            
    def on_inbox_file_register_failed(self, err, stream_id):
        lg.out(2, 'udp_file_queue.on_inbox_file_register_failed ERROR failed to register, stream_id=%s, err: %s' % (str(stream_id), err))
        self.session.automat('shutdown')

    def on_outbox_file_registered(self, response, stream_id):
        try:
            transfer_id = int(response)
        except:
            transfer_id = None
        self.outboxFiles[stream_id].transfer_id = transfer_id
        self.outboxFiles[stream_id].registration = None
        if self.outboxFiles[stream_id].is_done():
            self.report_outbox_file(self.outboxFiles[stream_id], 'finished')
            self.close_stream(stream_id)
            self.close_outbox_file(stream_id)
        elif self.outboxFiles[stream_id].is_cancelled():
            self.report_outbox_file(self.outboxFiles[stream_id], 'failed', 'cancelled')
            self.close_stream(stream_id)
            self.close_outbox_file(stream_id)
        elif self.outboxFiles[stream_id].is_timed_out():
            self.report_outbox_file(self.outboxFiles[stream_id], 'failed', 'timeout sending')
            self.close_stream(stream_id)
            self.close_outbox_file(stream_id)

    def on_outbox_file_register_failed(self, err, stream_id):
        lg.out(2, 'udp_file_queue.on_outbox_file_register_failed ERROR failed to register, stream_id=%s :\n%s' % (str(stream_id), str(err)))
        lg.out(6, 'udp_file_queue.on_outbox_file_register_failed close session %s' % self.session)
        self.session.automat('shutdown')

    def on_failed_outbox_queue_item(self, filename, description='', error_message='', result_defer=None, single=False):
        lg.out(18, 'udp_file_queue.failed_outbox_queue_item %s because %s' % (filename, error_message))
        if not single:
            udp_interface.interface_cancelled_file_sending(
                self.session.peer_id, filename, 0, description, error_message)
        if result_defer:
            result_defer.callback(((filename, description), 'failed', error_message))

    def on_timeout_receiving(self, stream_id):
        lg.out(18, 'udp_file_queue.on_timeout_receiving stream_id=%s ' % stream_id)
        assert stream_id in self.inboxFiles.keys()
        self.inboxFiles[stream_id].timeout = True
        if self.inboxFiles[stream_id].registration:
            return
        if self.inboxFiles[stream_id].transfer_id:
            self.report_inbox_file(self.inboxFiles[stream_id], 'failed', 'timeout receiving')
        self.close_stream(stream_id)
        self.close_inbox_file(stream_id)

    def on_timeout_sending(self, stream_id):
        lg.out(18, 'udp_file_queue.on_timeout_sending stream_id=%s ' % stream_id)
        assert stream_id in self.outboxFiles.keys()
        self.outboxFiles[stream_id].timeout = True
        if self.outboxFiles[stream_id].registration:
            return
        if self.outboxFiles[stream_id].transfer_id:
            self.report_outbox_file(self.outboxFiles[stream_id], 'failed', 'timeout sending')
        self.close_stream(stream_id)
        self.close_outbox_file(stream_id)

#------------------------------------------------------------------------------ 

class InboxFile():
    def __init__(self, queue, stream_id, size):
        """
        """
        self.transfer_id = None
        self.registration = None
        self.queue = queue
        self.stream_id = stream_id
        self.fd, self.filename = tmpfile.make("udp-in")
        self.size = size
        self.bytes_received = 0
        self.started = time.time()
        self.cancelled = False
        self.timeout = False
        # self.timeout = max(float(self.size)/settings.ReceivingSpeedLimit(), 5.0)
        # lg.out(18, 'udp_file_queue.InboxFile.__init__ {%s} [%d] from %s' % (
        #     os.path.basename(self.filename), self.stream_id, str(self.queue.session.peer_address)))
        
    def __del__(self):
        """
        """
        # lg.out(18, 'udp_file_queue.InboxFile.__del__ {%s} [%d]' % (os.path.basename(self.filename), self.stream_id,))

    def close(self):
        lg.out(18, 'udp_file_queue.InboxFile.close %d : %d received' % (
            self.stream_id, self.bytes_received))
        self.close_file()
        self.queue = None

    def close_file(self):
        os.close(self.fd)
        self.fd = None

    def process(self, newdata):
        os.write(self.fd, newdata)
        self.bytes_received += len(newdata)
        
    def is_done(self):
        # print 'is done', self.bytes_received, self.size
        return self.bytes_received == self.size
    
    def is_timed_out(self):
        return self.timeout # self.timeout == -1

    def on_received_raw_data(self, newdata):
        self.process(newdata)

#------------------------------------------------------------------------------ 

class OutboxFile():
    def __init__(self, queue, stream_id, filename, size, description='', 
                 result_defer=None, single=False):
        """
        """
        self.transfer_id = None
        self.registration = None
        self.queue = queue
        self.stream = None
        self.stream_id = stream_id
        self.filename = filename
        self.size = size
        self.description = description
        self.result_defer = result_defer
        self.single = single
        self.bytes_sent = 0
        self.bytes_delivered = 0
        self.buffer = ''
        self.eof = False
        self.cancelled = False
        self.timeout = False
        self.started = time.time()
        # self.timeout = max(float(self.size)/settings.SendingSpeedLimit(), 5.0)
        self.fileobj = open(self.filename, 'rb')
        # lg.out(18, 'udp_file_queue.OutboxFile.__init__ {%s} [%d] to %s' % (
        #     os.path.basename(self.filename), self.stream_id, str(self.queue.session.peer_address)))

    def __del__(self):
        """
        """
        # lg.out(18, 'udp_file_queue.OutboxFile.__del__ {%s} [%d] file:%r' % (
        #     os.path.basename(self.filename), self.stream_id, self.fileobj))

    def close(self):
        lg.out(18, 'udp_file_queue.OutboxFile.close %s %d/%d' % (
            self.stream_id, self.bytes_sent, self.bytes_delivered))
        if self.fileobj:
            self.close_file()
        self.queue = None
        self.buffer = ''
        self.description = None
        self.result_defer = None

    def close_file(self):
        # if self.fileobj:
        self.fileobj.close()
        self.fileobj = None

    def is_done(self):
        return self.eof and self.size == self.bytes_delivered
    
    def is_cancelled(self):
        return self.cancelled
        # return self.eof and self.bytes_sent >= self.size and self.size != self.bytes_delivered and self.timeout > 0
    
    def is_timed_out(self):
        return self.timeout # self.timeout == -1
    
    def count_size(self, more_bytes_delivered):
        self.bytes_delivered += more_bytes_delivered
    
    def cancel(self):
        lg.out(18, 'udp_file_queue.OutboxFile.cancel %s %d/%d' % (
            self.stream_id, self.bytes_sent, self.bytes_delivered))
        self.cancelled = True
        self.eof = True
        self.close_file()
    
    def process(self):
        if self.eof:
            return False
        has_sends = False
        while True:
            if not self.buffer:
                self.buffer = self.fileobj.read(udp_stream.BUFFER_SIZE)
                if not self.buffer:
                    # print 'EOF!!!', self.filename
                    self.eof = True
                    self.close_file()
                    break
            try:
                self.stream.write(self.buffer)
            except udp_stream.BufferOverflow:
                break
            self.bytes_sent += len(self.buffer)
            self.buffer = ''
            has_sends = True
        return has_sends
          
    def on_sent_raw_data(self, bytes_delivered):
        self.count_size(bytes_delivered)
        if self.is_done():
            lg.out(18, 'on_sent_raw_data: %s' % self.stream_id)
            self.queue.on_outbox_file_done(self, 'finished')
            return True
        self.process()
        return False
    
    def on_zero_ack(self, bytes_left):
        # print 'on_zero_ack', bytes_left, self.size, self.bytes_delivered,
        self.count_size(bytes_left)
        if self.is_done():
            lg.out(18, 'on_zero_ack done: %s' % self.stream_id)
            self.queue.on_outbox_file_done(self, 'finished')
        else:
            self.queue.on_outbox_file_done(self, 'failed', 'transfer interrupted')
        # print status
        return True

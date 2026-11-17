"""Bounded JSON RPC over FA013-owned Windows/Linux process transport.

This module never creates a process/daemon, opens a socket, or resolves a key.
FA010 binds channel_factory to the existing reviewed owned-process lifecycle.
"""
import struct
import time
from .protocol import strict_json, encoded, require, digest, MAX_REQUEST, MAX_RESPONSE, Rejected

MAX_BRIDGE_FRAME=64*1024*1024


class FramedChannel:
    def __init__(self, reader, writer):
        self.reader = reader; self.writer = writer

    def _read_exact(self, count):
        result = bytearray()
        while len(result) < count:
            data = self.reader.read(count-len(result))
            require(bool(data), 'BRIDGE_EOF')
            result.extend(data)
        return bytes(result)

    def receive(self):
        length = struct.unpack('>I', self._read_exact(4))[0]
        require(length <= MAX_BRIDGE_FRAME, 'BRIDGE_FRAME_TOO_LARGE')
        return strict_json(self._read_exact(length), MAX_BRIDGE_FRAME)

    def send(self, value):
        raw = encoded(value)
        require(len(raw) <= MAX_BRIDGE_FRAME, 'BRIDGE_FRAME_TOO_LARGE')
        self.writer.write(struct.pack('>I', len(raw))+raw); self.writer.flush()


class WindowsResearchBridge:
    ALLOWED = {'active', 'model_call', 'export_input', 'resolve_roles', 'committed_history',
               'reserve_prefix', 'commit_stage_artifacts', 'controller_record', 'progress'}

    def __init__(self, *, channel_factory, host_ports):
        require(set(host_ports) == self.ALLOWED, 'HOST_PORTS_INCOMPLETE')
        self.channel_factory = channel_factory; self.host_ports = host_ports

    @staticmethod
    def _message(message,expected,sequence):
        require(type(message)is dict and message.get('binding_sha256')==expected
                and type(message.get('sequence'))is int and message['sequence']==sequence,'BRIDGE_BINDING_MISMATCH')
        if message.get('type')=='outcome':
            require(set(message)=={'type','binding_sha256','sequence','value'},'BRIDGE_MESSAGE_INVALID')
        else:
            require(set(message)=={'type','binding_sha256','sequence','operation','value'}
                    and message['type']=='call','BRIDGE_MESSAGE_INVALID')
        return message

    def _cleanup(self,channel,context,begun,expected,sequence,current,returned,response):
        """Finish retained I/O once; only existing guarded evidence may ACK."""
        pending=channel.resume_pending()
        received=None
        if pending is not None:
            require(type(pending)is dict and set(pending)=={'kind','payload','value'},'BRIDGE_PENDING_IO_INVALID')
            if pending['kind']=='send':
                require(pending['payload']==response,'BRIDGE_PENDING_SEND_MISMATCH')
                # Original send completed. It may already have reached Linux;
                # never resend it or send a second denial for this sequence.
                if current is not None and current.get('operation')=='controller_record' and current['value'].get('event')=='HELPER_CLOSED':
                    return
                current=None;returned=False;response=None
            else:
                require(pending['kind']=='receive' and current is None,'BRIDGE_PENDING_IO_INVALID')
                received=pending['value']
        while channel.cleanup_remaining()>0:
            if current is None:
                message=received if received is not None else channel.receive()
                received=None
                current=self._message(message,expected,sequence);sequence+=1;returned=False
                if current['type']=='call':channel.note_request(current)
            # No business outcome, successful or failed, can resume a stopped
            # business path. Final proof arrives only through HELPER_CLOSED.
            require(current['type']=='call','BRIDGE_CLEANUP_OUTCOME_FORBIDDEN')
            operation=current['operation'];require(operation in self.ALLOWED,'BRIDGE_OPERATION_UNSUPPORTED')
            helper=False
            try:
                if operation=='controller_record':
                    if returned and current['value'].get('event')=='CREATED_INSPECTED_NOT_STARTED':
                        # Registration committed before the post-call cutoff,
                        # but no ACK was sent: evidence cannot authorize start.
                        self.host_ports['active'](context,begun,None)
                        raise Rejected('BRIDGE_CLEANUP_GUARD_INVALID')
                    value=response['value'] if returned else self.host_ports[operation](context,begun,current['value'])
                    require(value=={'recorded':True},'BRIDGE_CLEANUP_ACK_INVALID')
                    helper=current['value'].get('event')=='HELPER_CLOSED'
                    reply={'type':'return','binding_sha256':expected,'sequence':current['sequence'],'value':value}
                else:
                    # The existing FA013 cleanup guard proves unchanged fence
                    # transactionally, then denies. Never retry a business port.
                    self.host_ports['active'](context,begun,None)
                    raise Rejected('BRIDGE_CLEANUP_GUARD_INVALID')
            except BaseException as error:
                code=getattr(error,'code',None) or (str(error) if type(error)is Rejected else None)
                if code!='BRIDGE_CLEANUP_ONLY':raise
                reply={'type':'error','binding_sha256':expected,'sequence':current['sequence'],'code':'BRIDGE_CLEANUP_ONLY'}
            channel.send(reply)
            if helper:return
            current=None;returned=False;response=None
        raise Rejected('BRIDGE_CLEANUP_WALL_EXCEEDED')

    def run_development(self, context, begun):
        require(begun['phase']=='development' and 'final_test' not in begun['spec'], 'FINAL_SCOPE_LEAK')
        return self._run(context,begun)

    def run_final(self, context, begun):
        seal=begun.get('sealed_candidate')
        require(begun['phase']=='final_test' and type(seal) is dict
                and seal.get('candidate_hash')==begun.get('candidate_hash')
                and seal.get('exposure_recorded') is True, 'FINAL_SEAL_REQUIRED')
        return self._run(context,begun)

    def _run(self, context, begun):
        binding = {'session_id': begun['session_id'], 'attempt_id': begun['attempt_id'],
                   'job_id': context.job_id, 'run_id': context.run.run_id, 'fencing_token': context.fencing_token}
        expected = digest(binding); sequence = 0
        # Factory must impose wall/cancel/read deadlines and proof of owned
        # helper closure. Plain blocking stdio is NOT such a factory by itself.
        with self.channel_factory(context, binding) as channel:
            expected_runtime=self.channel_factory.state.runtime_sha256
            require(type(expected_runtime) is str and len(expected_runtime)==64,'BRIDGE_RUNTIME_REQUIRED')
            current=None;returned=False
            response={'schema': 'fa-rpc-start-1', 'binding': binding, 'begun': begun,'runtime_sha256':expected_runtime}
            try:
                channel.send(response);response=None
                while True:
                    context.require_active()
                    current=self._message(channel.receive(),expected,sequence);sequence+=1;returned=False
                    if current['type']=='outcome':return current['value']
                    channel.note_request(current)
                    operation=current['operation'];require(operation in self.ALLOWED,'BRIDGE_OPERATION_UNSUPPORTED')
                    require(begun['phase']!='final_test' or operation not in {'model_call','resolve_roles','committed_history'}, 'FINAL_ADAPTIVE_PORT_FORBIDDEN')
                    value=self.host_ports[operation](context,begun,current['value']);returned=True
                    response={'type':'return','binding_sha256':expected,'sequence':current['sequence'],'value':value}
                    context.require_active();channel.send(response)
                    current=None;returned=False;response=None
            except BaseException as primary:
                if not all(callable(getattr(channel,name,None)) for name in
                           ('begin_cleanup','record_cleanup_error','cleanup_remaining','resume_pending')):raise
                try:
                    channel.begin_cleanup(primary)
                    self._cleanup(channel,context,begun,expected,sequence,current,returned,response)
                except BaseException as cleanup:
                    try:channel.record_cleanup_error(cleanup)
                    except BaseException as persist_error:
                        primary.add_note('cleanup_error_record_failed:'+type(persist_error).__name__)
                raise  # Keep original failure; receipt-only close cannot succeed business.


class LinuxHostRPC:
    def __init__(self, channel, binding,*,cleanup_budget=None,clock=None,on_cleanup=None):
        self.channel = channel; self.binding = binding; self.sequence = 0
        self.cleanup_budget=cleanup_budget
        self.clock=clock if clock is not None else cleanup_budget.clock if cleanup_budget is not None else time.monotonic
        self.on_cleanup=on_cleanup
        self.cleanup_latched=False

    def call(self, operation, value=None):
        require(operation in WindowsResearchBridge.ALLOWED, 'BRIDGE_OPERATION_UNSUPPORTED')
        seq = self.sequence; self.sequence += 1
        sent_at=self.clock()
        try:
            if self.cleanup_budget is not None:self.cleanup_budget.check()
            self.channel.send({'type': 'call', 'binding_sha256': digest(self.binding), 'sequence': seq,
                               'operation': operation, 'value': value})
            reply = self.channel.receive()
            require(type(reply)is dict and reply.get('binding_sha256')==digest(self.binding)
                    and type(reply.get('sequence'))is int and reply['sequence']==seq,'BRIDGE_RETURN_MISMATCH')
            if reply.get('type')=='error':
                require(set(reply)=={'type','binding_sha256','sequence','code','cleanup_clock_version','cleanup_remaining_ms','cleanup_processing_elapsed_ms'}
                        and reply['code']=='BRIDGE_CLEANUP_ONLY' and type(reply['cleanup_clock_version'])is int
                        and reply['cleanup_clock_version']==2,'BRIDGE_RETURN_MISMATCH')
                self._remaining(reply['cleanup_remaining_ms'],sent_at,reply['cleanup_processing_elapsed_ms'])
                raise Rejected('BRIDGE_CLEANUP_ONLY')
            require(reply.get('type')=='return','BRIDGE_RETURN_MISMATCH')
            if 'cleanup_remaining_ms'in reply:
                require(set(reply)=={'type','binding_sha256','sequence','value','cleanup_clock_version','cleanup_remaining_ms','cleanup_processing_elapsed_ms'}
                        and type(reply['cleanup_clock_version'])is int and reply['cleanup_clock_version']==2
                        and operation=='controller_record' and type(value)is dict
                        and value.get('event')in {'CONTAINER_STOPPED','FAILED_ATTEMPT_RETAINED_STOPPED','HELPER_CLOSED'}
                        and reply['value']=={'recorded':True},'BRIDGE_RETURN_MISMATCH')
                self._remaining(reply['cleanup_remaining_ms'],sent_at,reply['cleanup_processing_elapsed_ms'])
            else:
                require(not self.cleanup_latched and set(reply)=={'type','binding_sha256','sequence','value'},'BRIDGE_RETURN_MISMATCH')
            if self.cleanup_budget is not None:self.cleanup_budget.check()
            return reply['value']
        except BaseException:
            if self.cleanup_budget is not None:self.cleanup_budget.begin()
            raise

    def _remaining(self,milliseconds,sent_at,processing_elapsed_ms):
        require(self.cleanup_budget is not None,'CLEANUP_BUDGET_REQUIRED')
        self.cleanup_budget.tighten(milliseconds,sent_at=sent_at,processing_elapsed_ms=processing_elapsed_ms);self.cleanup_latched=True
        if self.on_cleanup is not None:self.on_cleanup()
        self.cleanup_budget.check()

    def outcome(self, value):
        if self.cleanup_budget is not None:self.cleanup_budget.check()
        require(not self.cleanup_latched,'BRIDGE_CLEANUP_ONLY')
        self.channel.send({'type': 'outcome', 'binding_sha256': digest(self.binding), 'sequence': self.sequence, 'value': value})
        if self.cleanup_budget is not None:self.cleanup_budget.check()

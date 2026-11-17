"""Concrete bindings to FA013 r04 services; no new registration or SQL here."""
import hashlib
from .protocol import require, strict_json, digest


def bind(runtime, sessions, inputs, model_broker):
    def active(context, begun, value):
        context.require_active()
        return {'active':True}

    def model_call(context, begun, value):
        require(type(value) is dict and value.get('attempt_id') == begun['attempt_id'], 'MODEL_ATTEMPT_BINDING_MISMATCH')
        return model_broker.call(context,value)

    def exported(context, begun, value):
        require(value is None, 'EXPORT_ARGUMENTS_NOT_ACCEPTED')
        return inputs.export(context,begun)

    def roles(context, begun, value):
        require(value is None, 'ROLE_ARGUMENTS_NOT_ACCEPTED')
        return begun['resolved']['model_profiles']

    def history(context, begun, value):
        require(value is None, 'HISTORY_ARGUMENTS_NOT_ACCEPTED')
        result=list(begun.get('development_history',[]))
        refs=begun.get('checkpoint_refs',[])
        if refs:
            principal=runtime.research_task_service.principal(context.run.owner_id)
            parts=[]
            for ref in refs:
                row=sessions.artifact(principal,begun['session_id'],ref['artifact_id'])
                raw=row['content'].encode()
                require(hashlib.sha256(raw).hexdigest()==ref['sha256'] and row['phase']=='development'
                        and row['kind']=='checkpoint' and row['full_sha256']==ref['full_sha256'], 'CHECKPOINT_BINDING_MISMATCH')
                parts.append(row)
            parts.sort(key=lambda p:p['part_index'])
            require([p['part_index'] for p in parts]==list(range(len(parts))) and all(p['part_count']==len(parts) for p in parts), 'CHECKPOINT_PARTS_INVALID')
            raw=''.join(p['content'] for p in parts).encode()
            require(hashlib.sha256(raw).hexdigest()==parts[0]['full_sha256'], 'CHECKPOINT_HASH_MISMATCH')
            checkpoint=strict_json(raw,16777216)
            result.append(checkpoint)
        return result

    def reserve(context,begun,value):
        require(set(value)=={'symbol','index','request_hash'}, 'PREFIX_ARGUMENTS_INVALID')
        return sessions.reserve_prefix(context,begun,value['symbol'],value['index'],value['request_hash'])

    def stage(context,begun,value):
        return sessions.commit_stage_artifacts(context,begun,value)

    def progress(context,begun,value):
        require(set(value)=={'stage'}, 'PROGRESS_ARGUMENTS_INVALID')
        return sessions.progress(context,value['stage'])

    def controller_event(context,begun,value):
        context.require_active()
        require(type(value) is dict and len(value)<=12,'CONTROLLER_EVENT_INVALID')
        # Authoritative create/close journal is persisted by Linux ControllerJournal.
        # This RPC acknowledgement is not presented as a second durable journal.
        return {'received_sha256':digest(value),'host_persisted':False,'authoritative':'LINUX_CONTROLLER_JOURNAL'}

    return {'active':active,'model_call':model_call,'export_input':exported,'resolve_roles':roles,
            'committed_history':history,'reserve_prefix':reserve,'commit_stage_artifacts':stage,
            'controller_record':controller_event,'progress':progress}

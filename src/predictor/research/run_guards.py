"""Persistent SQLite guards, usable by public and ordinary sqlite3 connections."""
from predictor.research.run_contract import RUN_REQUIRED

def _timestamp(column):
    return f"(typeof({column})='text' AND ({column} GLOB '????-??-??T??:??:??*Z' OR {column} GLOB '????-??-??T??:??:??*+00:00') AND julianday({column}) IS NOT NULL AND date({column},'+0 days')=substr({column},1,10))"

def statements():
    required = ','.join("'"+k+"'" for k in sorted(RUN_REQUIRED | {'configuration_hash'}))
    # Each check is a separate statement: malformed JSON is rejected before JSON traversal.
    metadata = f"""
    CREATE TRIGGER IF NOT EXISTS guard_runs_insert BEFORE INSERT ON runs BEGIN
      SELECT RAISE(ABORT,'Invalid run metadata JSON') WHERE NOT json_valid(NEW.metadata);
      SELECT RAISE(ABORT,'Invalid run metadata fields') WHERE
        json_type(NEW.metadata) IS NOT 'object' OR
        (SELECT count(DISTINCT key) FROM json_each(NEW.metadata) WHERE key IN ({required})) != {len(RUN_REQUIRED)+1} OR
        EXISTS (SELECT 1 FROM json_tree(NEW.metadata) WHERE key IS NOT NULL GROUP BY parent,key HAVING count(*)>1) OR
        json_type(NEW.metadata,'$.experiment_id') IS NOT 'text' OR json_extract(NEW.metadata,'$.experiment_id') IS NOT NEW.run_id OR
        json_type(NEW.metadata,'$.hypothesis') IS NOT 'text' OR length(trim(json_extract(NEW.metadata,'$.hypothesis')))=0 OR
        json_type(NEW.metadata,'$.decision_before_test') IS NOT 'text' OR length(trim(json_extract(NEW.metadata,'$.decision_before_test')))=0 OR
        json_type(NEW.metadata,'$.kind') IS NOT 'text' OR json_extract(NEW.metadata,'$.kind') NOT IN ('engineering','development','historical','prospective') OR
        json_type(NEW.metadata,'$.uses_market_data') NOT IN ('true','false') OR
        (json_extract(NEW.metadata,'$.kind')='engineering' AND json_type(NEW.metadata,'$.uses_market_data')!='false') OR
        json_type(NEW.metadata,'$.configuration') IS NOT 'object' OR
        json_type(NEW.metadata,'$.failure') IS NOT 'null' OR json_type(NEW.metadata,'$.compute_time') IS NOT 'null' OR
        json_type(NEW.metadata,'$.all_metrics') IS NOT 'object' OR json_type(NEW.metadata,'$.seed') NOT IN ('integer','null') OR
        json_type(NEW.metadata,'$.search_parent') NOT IN ('text','null') OR
        (json_type(NEW.metadata,'$.search_parent')='text' AND NOT EXISTS(SELECT 1 FROM runs WHERE run_id=json_extract(NEW.metadata,'$.search_parent')));
    """
    for field in ('code_hash','configuration_hash','data_hash','model_hash','prompt_hash_if_any'):
        null_allowed = field in {'data_hash','model_hash','prompt_hash_if_any'}
        condition = f"(json_type(NEW.metadata,'$.{field}') IS NOT 'text' OR length(json_extract(NEW.metadata,'$.{field}'))!=64 OR json_extract(NEW.metadata,'$.{field}') GLOB '*[^0-9a-f]*')"
        if null_allowed: condition=f"(json_type(NEW.metadata,'$.{field}') IS NOT 'null' AND {condition})"
        metadata += f"SELECT RAISE(ABORT,'Invalid digest') WHERE {condition};\n"
    for field in ('data','model'):
        condition=f"(json_type(NEW.metadata,'$.{field}_hash_reason') IS NOT 'text' OR length(trim(json_extract(NEW.metadata,'$.{field}_hash_reason')))=0)"
        if field=='data':condition=f"({condition} OR json_extract(NEW.metadata,'$.kind')!='engineering' OR json_type(NEW.metadata,'$.uses_market_data')!='false')"
        metadata+=f"SELECT RAISE(ABORT,'Unexplained absent {field}') WHERE json_type(NEW.metadata,'$.{field}_hash')='null' AND {condition};\n"
    metadata += """
      SELECT RAISE(ABORT,'Invalid formal information cutoff') WHERE
        (json_extract(NEW.metadata,'$.kind') IN ('historical','prospective') AND json_type(NEW.metadata,'$.information_cutoff')='null');
    """
    for field in ('information_cutoff','train_label_maturity_max','decision_recorded_at_utc'):
        column=f"json_extract(NEW.metadata,'$.{field}')"
        guard=f"json_type(NEW.metadata,'$.{field}') IS NOT 'null'" if field!='decision_recorded_at_utc' else f"json_type(NEW.metadata,'$.{field}') IS NOT NULL"
        metadata+=f"SELECT RAISE(ABORT,'Invalid run timestamp') WHERE {guard} AND NOT COALESCE({_timestamp(column)},0);\n"
    metadata += """
      SELECT RAISE(ABORT,'Invalid temporal order') WHERE
        (json_type(NEW.metadata,'$.train_label_maturity_max')!='null' AND
          (json_type(NEW.metadata,'$.information_cutoff')='null' OR julianday(json_extract(NEW.metadata,'$.train_label_maturity_max'))>julianday(json_extract(NEW.metadata,'$.information_cutoff')))) OR
        (json_extract(NEW.metadata,'$.kind') IN ('historical','prospective') AND julianday(json_extract(NEW.metadata,'$.information_cutoff'))>julianday('now')) OR
        (json_type(NEW.metadata,'$.decision_recorded_at_utc') IS NOT NULL AND julianday(json_extract(NEW.metadata,'$.decision_recorded_at_utc'))>julianday('now')) OR
        (json_extract(NEW.metadata,'$.kind') IN ('historical','prospective') AND lower(COALESCE(json_extract(NEW.metadata,'$.recording_mode'),'')) LIKE '%retrospect%');
    END
    """
    event = f"""
    CREATE TRIGGER IF NOT EXISTS guard_run_events_insert BEFORE INSERT ON run_events BEGIN
      SELECT RAISE(ABORT,'Invalid run event timestamp') WHERE NOT COALESCE({_timestamp('NEW.recorded_at_utc')},0);
      SELECT RAISE(ABORT,'Invalid run event JSON') WHERE NOT json_valid(NEW.payload);
      SELECT RAISE(ABORT,'Invalid run event object') WHERE json_type(NEW.payload) IS NOT 'object' OR
        EXISTS (SELECT 1 FROM json_tree(NEW.payload) WHERE key IS NOT NULL GROUP BY parent,key HAVING count(*)>1);
      SELECT RAISE(ABORT,'Invalid run event state') WHERE NEW.status NOT IN ('running','succeeded','failed','interrupted') OR
        NOT EXISTS(SELECT 1 FROM runs WHERE run_id=NEW.run_id);
      SELECT RAISE(ABORT,'Invalid initial or terminal run transition') WHERE
        (NOT EXISTS(SELECT 1 FROM run_events WHERE run_id=NEW.run_id) AND NEW.status!='running') OR
        (EXISTS(SELECT 1 FROM run_events WHERE run_id=NEW.run_id) AND
          (NEW.status='running' OR (SELECT status FROM run_events WHERE run_id=NEW.run_id ORDER BY event_id DESC LIMIT 1)!='running' OR
           (SELECT count(*) FROM run_events WHERE run_id=NEW.run_id)!=1)) OR
        (NEW.event_id!=-1 AND EXISTS(SELECT 1 FROM run_events WHERE event_id>=NEW.event_id));
      SELECT RAISE(ABORT,'Run event time goes backwards') WHERE EXISTS(
        SELECT 1 FROM run_events WHERE run_id=NEW.run_id AND julianday(recorded_at_utc)>julianday(NEW.recorded_at_utc));
      SELECT RAISE(ABORT,'Run registration predates declared information/decision') WHERE NEW.status='running' AND EXISTS(
        SELECT 1 FROM runs WHERE run_id=NEW.run_id AND (
          (json_extract(metadata,'$.kind') IN ('historical','prospective') AND
           julianday(json_extract(metadata,'$.information_cutoff'))>julianday(NEW.recorded_at_utc)) OR
          julianday(json_extract(metadata,'$.decision_recorded_at_utc'))>julianday(NEW.recorded_at_utc)));
      SELECT RAISE(ABORT,'Invalid running payload') WHERE NEW.status='running' AND EXISTS(SELECT 1 FROM json_each(NEW.payload));
      SELECT RAISE(ABORT,'Invalid terminal result') WHERE NEW.status!='running' AND (
        json_type(NEW.payload,'$.all_metrics') IS NOT 'object' OR
        json_type(NEW.payload,'$.failure') IS NULL OR
        json_type(NEW.payload,'$.compute_time') IS NULL OR json_type(NEW.payload,'$.compute_time') NOT IN ('integer','real') OR
        json_extract(NEW.payload,'$.compute_time')<0 OR json_extract(NEW.payload,'$.compute_time')>1.7976931348623157e308 OR
        (NEW.status='succeeded' AND json_type(NEW.payload,'$.failure')!='null') OR
        (NEW.status IN ('failed','interrupted') AND (json_type(NEW.payload,'$.failure')!='text' OR length(trim(json_extract(NEW.payload,'$.failure')))=0)));
    END
    """
    ordering = """CREATE TRIGGER IF NOT EXISTS guard_run_events_id AFTER INSERT ON run_events BEGIN
      SELECT RAISE(ABORT,'Invalid run event ID ordering') WHERE NEW.event_id<1 OR
        EXISTS(SELECT 1 FROM run_events WHERE event_id>NEW.event_id);
    END"""
    return [metadata,event,ordering]

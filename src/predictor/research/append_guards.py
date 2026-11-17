"""Persistent identity-conflict guards, independent of recursive_triggers.

Use only for logically immutable definitions/events. Derived views and writable
state are deliberately excluded. UPDATE/DELETE guards remain necessary.
"""
import re

def install(connection, tables, *, prefix='identity_v3'):
    for table in tables:
        if not re.fullmatch(r'[a-z_]+', table):
            raise ValueError('Invalid guard table')
        groups = []
        columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        primary = [r[1] for r in sorted(columns, key=lambda r:r[5]) if r[5]]
        if primary: groups.append(primary)
        for index in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
            if index[2]:
                keys = [r[2] for r in connection.execute('PRAGMA index_info("'+index[1]+'")')]
                if not all(isinstance(k,str) and re.fullmatch('[a-z_]+',k) for k in keys):
                    raise ValueError('Expression index requires explicit conflict guard')
                groups.append(keys)
        # All supported tables are rowid tables. Protect explicitly supplied
        # rowids too; the normal auto-rowid sentinel cannot collide with our
        # positive event IDs. Null/negative legacy identities need review.
        predicates=['rowid=NEW.rowid']
        predicates += ['('+' AND '.join(f'"{k}" IS NEW."{k}"' for k in group)+')' for group in groups]
        connection.execute(f'''CREATE TRIGGER IF NOT EXISTS {prefix}_{table}
            BEFORE INSERT ON "{table}" WHEN EXISTS(
                SELECT 1 FROM "{table}" WHERE {' OR '.join(predicates)})
            BEGIN SELECT RAISE(ABORT,'Append-only identity conflict'); END''')

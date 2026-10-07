"""Previewed, ownership-based removal of application data.

The client chooses a scope, never a filesystem path. Files are staged on their
own volume before database changes commit. Changed, linked, untracked, model,
and application files are never swept into a deletion plan.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import secrets
import re
import threading
import time

SCOPES = {'chats': 'DELETE CHAT SESSIONS', 'all': 'CLEAR APPLICATION DATA'}
WEIGHT_SUFFIXES = {'.gguf', '.safetensors', '.pt', '.pth', '.ckpt', '.onnx', '.bin'}
JOURNAL_KEY = 'data.cleanup_journal'


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _linked(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)


def _linked_ancestor(path: Path) -> bool:
    return any(_linked(part) for part in (path,*path.parents) if part.exists())


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


class DataManagement:
    def __init__(self, application):
        self.app = application
        self._lock = threading.RLock()
        self._plans = {}
        self._model_paths = []

    @contextmanager
    def _exclusive(self):
        """Reject busy cleanup instead of waiting behind a long model turn.

        All acquisitions are nonblocking, so a worker holding locks in another
        order cannot deadlock maintenance. The database transaction below also
        prevents new operations from starting while files are staged.
        """
        with ExitStack() as acquired:
            for lock in (self._lock, self.app.chat._generation_lock,
                         self.app.chat._bundle_lifecycle_lock,
                         self.app.automation._lock, self.app.chat.memory._lock,
                         self.app.chat.mission_memory._lock):
                if not lock.acquire(blocking=False):
                    raise RuntimeError('The application is busy; retry cleanup after active work finishes')
                acquired.callback(lock.release)
            yield

    def _database_fingerprint(self, scope):
        digest = hashlib.sha256()
        tables = ['conversations', 'messages', 'conversation_labels',
                  'conversation_label_links', 'chat_artifacts', 'image_generation_jobs',
                  'automation_audit_records']
        if scope == 'all':
            tables += ['operations', 'datasets', 'prepared_datasets', 'evaluations',
                       'notifications', 'saved_versions']
        with self.app.database.connection() as connection:
            for table in tables:
                digest.update(table.encode())
                # Only internal table names, never caller-supplied identifiers.
                for row in connection.execute(f'SELECT * FROM {table} ORDER BY rowid'):
                    digest.update(json.dumps(dict(row), sort_keys=True).encode())
            for row in connection.execute("SELECT key,value FROM application_metadata WHERE key GLOB 'ui.workspace.*' ORDER BY key"):
                digest.update(json.dumps(dict(row), sort_keys=True).encode())
        if scope == 'all':
            for memory in (self.app.chat.memory, self.app.chat.mission_memory):
                digest.update(str(memory.cleanup_revision()).encode())
        return digest.hexdigest()

    def _idle(self, connection):
        if connection.execute('SELECT 1 FROM application_metadata WHERE key=?', (JOURNAL_KEY,)).fetchone():
            raise RuntimeError('An interrupted cleanup needs recovery; restart the application')
        if connection.execute("SELECT 1 FROM operations WHERE state IN ('queued','running','stop_requested') LIMIT 1").fetchone():
            raise RuntimeError('Finish or stop active tasks before clearing data')
        if connection.execute("SELECT 1 FROM automation_audit_records WHERE outcome='pending' LIMIT 1").fetchone():
            raise RuntimeError('An automation action is still active')

    def _protected(self, path):
        paths = self.app.paths
        protected = [paths.workspace / 'models', paths.versions, paths.runtime, paths.tokenizer, *self._model_paths]
        protected += [paths.project_root / name for name in ('app', 'config', '.python', '.venv', '.git', 'tools', 'node_modules')]
        protected += [Path(str(database) + suffix)
                      for database in (paths.database, paths.database.parent / 'salty-memory.db')
                      for suffix in ('', '-wal', '-shm')]
        return path.suffix.casefold() in WEIGHT_SUFFIXES or any(_inside(path, root.resolve()) for root in protected)

    def _refresh_model_paths(self):
        app = self.app
        self._model_paths = []
        broad = {app.paths.workspace.resolve(),app.paths.project_root.resolve(),app.paths.training.resolve(),app.paths.cache.resolve()}
        for row in app.database.fetch_all('SELECT checkpoint_path FROM saved_versions'):
            path = Path(row['checkpoint_path']).resolve()
            self._model_paths.append(path if path.is_dir() or path.parent in broad else path.parent)

    @contextmanager
    def _durable_transaction(self):
        # The control plane normally uses NORMAL durability. File moves require
        # the intent and commit decision to survive before touching the filesystem.
        with self.app.database.connection() as connection:
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('BEGIN IMMEDIATE')
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def recovery_pending(self):
        return self.app.database.fetch_one('SELECT 1 FROM application_metadata WHERE key=?', (JOURNAL_KEY,)) is not None

    def _save_journal(self, connection, journal):
        connection.execute('INSERT INTO application_metadata(key,value) VALUES(?,?) '
                           'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           (JOURNAL_KEY, json.dumps(journal)))

    def _prepare_journal(self, plan, token):
        journal = {'version':1, 'state':'prepared', 'scope':plan['scope'], 'token':token,
                   'workspace':str(self.app.paths.workspace.resolve()), 'files':[]}
        for index, item in enumerate(plan['files']):
            source = Path(item['path'])
            journal['files'].append({**item, 'digest':_digest(source),
                'staged':str(Path(item['root'])/'.salty-cleanup'/token/str(index))})
        with self._durable_transaction() as connection:
            self._idle(connection)
            self._save_journal(connection, journal)
        return journal

    def _clear_memories(self):
        # Recovery runs before workers, model loading, or request handling.
        chat = getattr(self.app, 'chat', None)
        if chat is not None:
            chat.memory.forget_all()
            chat.mission_memory.clear()
            return
        from ..memory import SemanticMemory, AutomationMissionMemory
        path = self.app.paths.database.parent / 'salty-memory.db'
        for memory_type, method in ((SemanticMemory,'forget_all'), (AutomationMissionMemory,'clear')):
            memory = memory_type(path)
            try:
                getattr(memory, method)()
            finally:
                memory.close()

    def recover(self):
        """Restore uncommitted moves or finish a committed erasure, idempotently.

        Never overwrite a new file at the original path. A conflict or changed
        staged file keeps the journal and blocks startup for explicit recovery.
        """
        row = self.app.database.fetch_one('SELECT value FROM application_metadata WHERE key=?', (JOURNAL_KEY,))
        if row is None:
            return {'files_removed':0, 'bytes_reclaimed':0}
        journal = json.loads(row['value'])
        if (journal.get('version') != 1 or journal.get('scope') not in SCOPES
                or journal.get('state') not in {'prepared','committed'}
                or journal.get('workspace') != str(self.app.paths.workspace.resolve())
                or not re.fullmatch(r'[A-Za-z0-9_-]{43}', str(journal.get('token','')))):
            raise RuntimeError('Cleanup recovery journal is invalid; no files were changed')
        self._refresh_model_paths()
        checked = []
        for index, item in enumerate(journal['files']):
            source, root, staged = Path(item['path']), Path(item['root']), Path(item['staged'])
            expected = root/'.salty-cleanup'/journal['token']/str(index)
            if (not source.is_absolute() or not root.is_absolute() or staged != expected
                    or not _inside(source, root) or '.salty-cleanup' in source.parts
                    or _linked_ancestor(source) or _linked_ancestor(staged)
                    or source.resolve() != source or root.resolve() != root or self._protected(source)):
                raise RuntimeError('Cleanup recovery path changed; manual recovery is required')
            if staged.exists():
                if not staged.is_file() or _digest(staged) != item['digest']:
                    raise RuntimeError('Staged cleanup data changed; preserved for manual recovery')
                if journal['state']=='prepared' and source.exists():
                    raise RuntimeError('Cleanup recovery would overwrite a new file; both copies are preserved')
            elif journal['state']=='prepared' and not source.exists():
                raise RuntimeError('Cleanup recovery cannot locate an original file; journal preserved')
            checked.append((source, staged, item))
        if journal['state']=='committed' and journal['scope']=='all' and not journal.get('memory_done'):
            self._clear_memories()
            journal['memory_done'] = True
            with self._durable_transaction() as connection:
                self._save_journal(connection, journal)
        removed = reclaimed = 0
        for source, staged, item in checked:
            if not staged.exists():
                continue
            if journal['state']=='prepared':
                source.parent.mkdir(parents=True, exist_ok=True)
                staged.rename(source)
            else:
                size = staged.stat().st_size
                staged.unlink()
                removed += 1
                reclaimed += size
        for directory in {staged.parent for _, staged, _ in checked}:
            try:
                directory.rmdir()
                directory.parent.rmdir()
            except OSError:
                pass
        with self._durable_transaction() as connection:
            connection.execute('PRAGMA secure_delete=ON')
            connection.execute('DELETE FROM application_metadata WHERE key=?', (JOURNAL_KEY,))
        return {'files_removed':removed, 'bytes_reclaimed':reclaimed}

    def _plan(self, scope):
        if scope not in SCOPES:
            raise ValueError('Unknown data clearing scope')
        app = self.app
        self._refresh_model_paths()
        conversations = [dict(row) for row in app.database.fetch_all('SELECT id,updated_at FROM conversations ORDER BY id')]
        message_state=app.database.fetch_one('SELECT COUNT(*) AS n, MAX(created_at) AS latest FROM messages')
        ids = {row['id'] for row in conversations}
        operations = [row['id'] for row in app.database.fetch_all('SELECT id,target_id FROM operations') if row['target_id'] in ids]
        audit_ids = set()
        def collect(value):
            if isinstance(value, dict):
                if isinstance(value.get('audit_record_id'), str): audit_ids.add(value['audit_record_id'])
                for child in value.values(): collect(child)
            elif isinstance(value, list):
                for child in value: collect(child)
        for row in app.database.fetch_all('SELECT technical_details_json FROM messages'):
            try: collect(json.loads(row['technical_details_json'] or '{}'))
            except (ValueError, TypeError): pass
        roots = [app.paths.conversations]
        if scope == 'all':
            roots += [app.paths.cache, app.paths.logs, app.paths.datasets, app.paths.prepared, app.paths.training, app.paths.evaluations]
            roots += [app.paths.workspace / name for name in ('automation','generated','runtime-cache','backups','release-evidence','rollback')]
            roots += [app.paths.database.parent / 'missions']
        files, preserved, skipped = {}, [], []
        def add(path, root, checksum=None):
            if not path.exists(): return
            if _linked_ancestor(path): skipped.append(str(path)); return
            resolved = path.resolve()
            if not _inside(resolved, root.resolve()) or self._protected(resolved):
                preserved.append(str(path)); return
            if not resolved.is_file(): return
            if checksum and _digest(resolved) != checksum:
                skipped.append(str(path)); return
            stat = resolved.stat()
            files[str(resolved)] = {'path':str(resolved),'root':str(root.resolve()),
                'bytes':stat.st_size,'mtime':stat.st_mtime_ns,'checksum':checksum}
        for declared in roots:
            declared = Path(declared)
            if not declared.exists(): continue
            if _linked(declared): skipped.append(str(declared)); continue
            root = declared.resolve()
            if root in {app.paths.workspace.resolve(), app.paths.project_root.resolve(), Path(root.anchor)}:
                raise ValueError('A data directory resolves to an unsafe broad root')
            if not _inside(root,app.paths.workspace.resolve()):
                raise ValueError('Bulk cleanup requires dedicated workspace storage; external files need audited ownership')
            for folder, directories, names in os.walk(root, followlinks=False):
                directories[:] = [name for name in directories if name != '.salty-cleanup' and not _linked(Path(folder)/name)
                                  and not self._protected((Path(folder)/name).resolve())]
                for name in names: add(Path(folder)/name, root)
        if scope == 'chats':
            root = app.paths.database.parent / 'missions'
            for operation in operations:
                for suffix in ('.json','-research.json'):
                    add(root/(operation+suffix),root)
        for row in app.database.fetch_all("SELECT id,capability,result_json FROM automation_audit_records WHERE outcome='succeeded'"):
            if scope == 'chats' and row['id'] not in audit_ids: continue
            try: result = json.loads(row['result_json'] or '{}')
            except ValueError: continue
            artifact = result.get('artifact') or {}
            if artifact.get('path') and artifact.get('sha256'):
                path = Path(artifact['path'])
                add(path, path.parent, artifact['sha256'])
            # Only files created by the application, never edits to pre-existing
            # user files. The original checksum must still match.
            if row['capability'] == 'files.manage' and result.get('created') and result.get('sha256'):
                for value in result.get('affected_paths') or []:
                    path = Path(value)
                    if path.is_absolute(): add(path, path.parent, result['sha256'])
        fingerprint = hashlib.sha256(json.dumps({'database':self._database_fingerprint(scope),
            'conversations':conversations,'messages':message_state,'files':files},sort_keys=True).encode()).hexdigest()
        return {'scope':scope,'conversations':conversations,'operations':operations,'audit_ids':sorted(audit_ids),
                'files':list(files.values()),'fingerprint':fingerprint,'preserved':preserved,'skipped':skipped}

    def preview(self, scope):
        with self._exclusive(), self.app.database.connection() as connection:
            self._idle(connection)
            plan = self._plan(scope)
            now = time.monotonic()
            self._plans = {key:value for key,value in self._plans.items() if value['expires'] > now}
            if len(self._plans) >= 8: self._plans.pop(next(iter(self._plans)))
            token = secrets.token_urlsafe(32)
            self._plans[token] = {**plan,'expires':now+600}
            return {'token':token,'scope':scope,'confirmation':SCOPES[scope],
                'conversation_count':len(plan['conversations']),'file_count':len(plan['files']),
                'memory_count':self.app.chat.memory.statistics()['active'] if scope=='all' else 0,
                'bytes':sum(item['bytes'] for item in plan['files']),
                'preserved_file_count':len(plan['preserved']),'skipped_file_count':len(plan['skipped']),
                'preserves':['Application and settings','Model weights, saved model versions and tokenizer',
                             'Untracked files and modified user files'] + (['Saved global memory'] if scope=='chats' else []),
                'scope_note':'Only registered application storage and unchanged, audited generated files are included. No drive-wide deletion.'}

    @staticmethod
    def _delete_audits(connection, ids=None):
        # Erasure is a deliberate maintenance transaction. Restore the normal
        # append-only trigger before committing, including after a failed delete.
        trigger = connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_automation_audit_append_only_delete'").fetchone()
        connection.execute('DROP TRIGGER IF EXISTS trg_automation_audit_append_only_delete')
        try:
            if ids is None: connection.execute('DELETE FROM automation_audit_records')
            else: connection.executemany('DELETE FROM automation_audit_records WHERE id=?',[(value,) for value in ids])
        finally:
            if trigger: connection.execute(trigger[0])

    def execute(self, token, confirmation):
        app = self.app
        with self._exclusive():
            plan = self._plans.get(str(token))
            if not plan or plan['expires'] < time.monotonic():
                raise ValueError('Cleanup preview expired; review the current data again')
            if confirmation != SCOPES[plan['scope']]:
                raise ValueError('Confirmation text does not match the selected scope')
            with app.database.connection() as connection:
                self._idle(connection)
            current = self._plan(plan['scope'])
            if current['fingerprint'] != plan['fingerprint']:
                raise RuntimeError('Data changed after preview; review a fresh preview')
            journal = self._prepare_journal(plan, token)
            try:
                with self._durable_transaction() as connection:
                    if connection.execute("SELECT 1 FROM operations WHERE state IN ('queued','running','stop_requested') LIMIT 1").fetchone():
                        raise RuntimeError('Active work started; cleanup cancelled')
                    if connection.execute("SELECT 1 FROM automation_audit_records WHERE outcome='pending' LIMIT 1").fetchone():
                        raise RuntimeError('An automation action started; cleanup cancelled')
                    current = self._plan(plan['scope'])
                    if current['fingerprint'] != plan['fingerprint']:
                        raise RuntimeError('Data changed after preview; review a fresh preview')
                    self._plans.pop(str(token), None)
                    for client in (getattr(app.automation,'_browser_client',None),getattr(app.automation,'_uia_client',None)):
                        if client: client.close()
                    if plan['scope']=='all':
                        with app.chat._bundle_lifecycle_lock:
                            if app.model_bundle_runtime: app.model_bundle_runtime.unload()
                            app.runtime.unload()
                    for index,item in enumerate(plan['files']):
                        source, root = Path(item['path']), Path(item['root'])
                        if _linked_ancestor(source) or source.resolve()!=source or self._protected(source):
                            raise RuntimeError('A planned file changed identity')
                        stat = source.stat()
                        if (stat.st_size,stat.st_mtime_ns)!=(item['bytes'],item['mtime']):
                            raise RuntimeError('A planned file changed; cleanup was cancelled')
                        if item['checksum'] and _digest(source)!=item['checksum']:
                            raise RuntimeError('An audited generated file changed')
                        if _digest(source) != journal['files'][index]['digest']:
                            raise RuntimeError('Planned cleanup content changed')
                        staging = root/'.salty-cleanup'
                        if staging.exists() and _linked(staging): raise RuntimeError('Cleanup staging folder is linked')
                        trash = staging/str(token)
                        if trash.exists() and _linked(trash): raise RuntimeError('Cleanup staging path is linked')
                        trash.mkdir(parents=True,exist_ok=True)
                        destination = trash/str(index)
                        if destination.exists():
                            raise RuntimeError('Cleanup destination already exists')
                        source.replace(destination)
                    connection.execute('PRAGMA secure_delete=ON')
                    connection.execute('DELETE FROM conversations')
                    connection.execute('DELETE FROM conversation_labels')
                    # Drafts and per-conversation instructions contain user data;
                    # keep appearance/preferences, but never resurrect a draft.
                    connection.execute("DELETE FROM application_metadata WHERE key GLOB 'ui.workspace.*'")
                    connection.execute("INSERT INTO application_metadata(key,value) VALUES('data.cleanup_epoch','1') "
                                       "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")
                    if plan['scope']=='all':
                        connection.execute('DELETE FROM evaluations')
                        connection.execute('DELETE FROM datasets')
                        connection.execute('DELETE FROM operations')
                        connection.execute('DELETE FROM notifications')
                        self._delete_audits(connection)
                    else:
                        connection.executemany('DELETE FROM operations WHERE id=?',[(value,) for value in plan['operations']])
                        self._delete_audits(connection,plan['audit_ids'])
                    journal['state'] = 'committed'
                    self._save_journal(connection, journal)
            except BaseException:
                self.recover()
                raise
            errors = []
            removed = reclaimed = 0
            try:
                result = self.recover()
                removed, reclaimed = result['files_removed'], result['bytes_reclaimed']
            except Exception as error:
                errors.append(f'Cleanup interrupted; restart to recover: {error}')
            try:
                with app.database.connection() as connection:
                    connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    connection.execute('VACUUM')
            except Exception as error: errors.append(f'Database compaction: {error}')
            return {'status':'partial' if errors else 'completed','scope':plan['scope'],
                    'conversations_removed':len(plan['conversations']),'files_removed':removed,
                    'bytes_reclaimed':reclaimed,'errors':errors,'restart_recommended':bool(errors) or plan['scope']=='all'}

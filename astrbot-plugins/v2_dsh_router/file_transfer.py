"""Binary QQ attachment import/export inside the active conversation's file domain.

Only canonical attachment references reach the resolver. No model-supplied URL,
OneBot destination, or host path is accepted by the download side.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
import stat
import uuid
import weakref
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

import aiohttp
try:
    from .console_settings import async_workspace_lock
except ImportError:
    import importlib.util
    _console_spec = importlib.util.spec_from_file_location(
        "v2_file_transfer_console_settings", Path(__file__).with_name("console_settings.py"))
    _console_module = importlib.util.module_from_spec(_console_spec)
    _console_spec.loader.exec_module(_console_module)
    async_workspace_lock = _console_module.async_workspace_lock

MEDIA = frozenset({'file', 'image', 'record', 'video'})
CHUNK = 256 * 1024
DEFAULT_MAX_BYTES = 1024 ** 3


class FileTransferError(ValueError):
    @property
    def public_message(self):
        return str(self)


class FileScopeError(PermissionError):
    @property
    def public_message(self):
        return str(self)


def safe_name(value: str) -> str:
    name = str(value).replace('\\', '/').rsplit('/', 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f]', '_', name).strip().strip('.')
    # Keep the extension, including for long non-ASCII client filenames.
    while len(name.encode('utf-8')) > 220:
        stem, ext = os.path.splitext(name)
        name = stem[:-1] + ext if stem else name[:-1]
    return name or 'attachment.bin'


class FileTransfer:
    _loop_storage_locks = weakref.WeakKeyDictionary()

    def storage_lock(self):
        loop = asyncio.get_running_loop()
        locks = self._loop_storage_locks.setdefault(loop, {})
        # A slow resource blocks only this conversation's own storage, not
        # another group or the Owner. Quota scans+copies share the same lock.
        return locks.setdefault(str(self.storage_root), asyncio.Lock())

    def __init__(self, journal, conversation, workspace, *, profile, resolve,
                 send, verify_root=None, max_bytes=None, spool_dir=None, storage_root=None,
                 workspace_lock_path=None):
        self.journal = journal
        self.conversation = conversation
        self.workspace = Path(workspace)
        self.storage_root = Path(storage_root) if storage_root else self.workspace
        self.profile = profile
        self.workspace_lock_path = workspace_lock_path
        self.trace = None
        self.history_after = 0
        self.conversation_quota = DEFAULT_MAX_BYTES
        self.host_reserve_bytes = DEFAULT_MAX_BYTES
        self.resolve = resolve
        self.send = send
        self.verify_root = verify_root
        self.spool_dir = Path(spool_dir) if spool_dir else self.workspace.parent / 'qq-file-spool'
        default_limit = DEFAULT_MAX_BYTES if profile != 'owner-host' else 2**63 - 1
        raw_limit = os.environ.get('V2_QQ_FILE_MAX_BYTES', str(default_limit)) if max_bytes is None else max_bytes
        self.max_bytes = int(raw_limit)
        if self.max_bytes <= 0:
            raise FileTransferError('QQ file transfer limit must be positive')

    @contextmanager
    def root(self):
        fd = self.verify_root() if self.verify_root else os.open(
            self.storage_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _relative(path):
        if not isinstance(path, str) or not path or '\x00' in path:
            raise FileTransferError('file path is required')
        if path.startswith('/workspace/'):
            path = path[len('/workspace/'):]
        parts = PurePosixPath(path).parts
        if not parts or PurePosixPath(path).is_absolute() or any(p in {'.', '..'} for p in parts):
            raise FileScopeError('file must be inside the current conversation workspace')
        return parts

    def _open_source(self, path):
        if self.profile == 'owner-host':
            target = Path(path)
            if not target.is_absolute():
                target = self.workspace / target
            fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
        else:
            parts = self._relative(path)
            if self.profile == 'safe-chat' and parts[0] != 'attachments':
                raise FileScopeError('safe-chat can only return its received attachments')
            with self.root() as root:
                root_dev = os.fstat(root).st_dev
                parent = os.dup(root)
                try:
                    for part in parts[:-1]:
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                        os.close(parent)
                        parent = child
                        if os.fstat(parent).st_dev != root_dev:
                            raise FileScopeError('file crosses a workspace mount')
                    fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                finally:
                    os.close(parent)
                if os.fstat(fd).st_dev != root_dev:
                    os.close(fd)
                    raise FileScopeError('file crosses a workspace mount')
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or (self.profile != 'owner-host' and meta.st_nlink != 1):
            os.close(fd)
            raise FileScopeError('only regular, unlinked-to-other-files workspace files can be sent')
        if meta.st_size > self.max_bytes:
            os.close(fd)
            raise FileTransferError('file exceeds configured transfer size limit')
        return fd

    @contextmanager
    def _new_file(self, area, name):
        # Fixed area names, random exclusively-created directory, dirfd writes:
        # renaming/symlink replacement cannot redirect the opened file to host.
        with self.root() as root:
            try:
                os.mkdir(area, mode=0o700, dir_fd=root)
            except FileExistsError:
                pass
            area_fd = os.open(area, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            folder = uuid.uuid4().hex
            folder_fd = fd = None
            complete = False
            try:
                if os.fstat(area_fd).st_dev != os.fstat(root).st_dev:
                    raise FileScopeError('attachment area crosses workspace mount')
                os.mkdir(folder, mode=0o700, dir_fd=area_fd)
                folder_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=area_fd)
                fd = os.open('.partial', os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                             0o600, dir_fd=folder_fd)
                yield fd, f'{area}/{folder}/{name}'
                os.fsync(fd)
                os.rename('.partial', name, src_dir_fd=folder_fd, dst_dir_fd=folder_fd)
                complete = True
            finally:
                if fd is not None:
                    os.close(fd)
                if folder_fd is not None:
                    if not complete:
                        try:
                            os.unlink('.partial', dir_fd=folder_fd)
                        except FileNotFoundError:
                            pass
                    os.close(folder_fd)
                if not complete:
                    try:
                        os.rmdir(folder, dir_fd=area_fd)
                    except OSError:
                        pass
                os.close(area_fd)

    def _stored_bytes(self):
        # safe-chat storage is private to the host; use directory descriptors
        # anyway so an unexpected link/mount fails rather than expanding scope.
        def total(directory, device):
            size = 0
            for name in os.listdir(directory):
                metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if metadata.st_dev != device:
                    raise FileScopeError('attachment storage crosses a mount')
                if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                    size += metadata.st_size
                elif stat.S_ISDIR(metadata.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    try:
                        size += total(child, device)
                    finally:
                        os.close(child)
                else:
                    raise FileScopeError('unsafe item in private attachment storage')
            return size
        with self.root() as root:
            metadata = os.fstat(root)
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise FileScopeError('private attachment storage must be owned and private')
            return total(root, metadata.st_dev)

    def _write(self, fd, chunk, state):
        state['size'] += len(chunk)
        if state.get('host_storage'):
            available = os.fstatvfs(fd)
            if available.f_bavail * available.f_frsize - len(chunk) < self.host_reserve_bytes:
                raise FileTransferError('host storage reserve would be exceeded')
        if 'remaining_quota' in state and state['size'] > state['remaining_quota']:
            raise FileTransferError('conversation attachment storage quota exceeded')
        if state['size'] > self.max_bytes:
            raise FileTransferError('file exceeds configured transfer size limit')
        state['hash'].update(chunk)
        view = memoryview(chunk)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError('attachment write made no progress')
            view = view[written:]

    async def receive(self, journal_id: int, segment_index: int):
        async with self.storage_lock():
            async with async_workspace_lock(self.workspace_lock_path):
                return await self._receive(journal_id, segment_index)

    async def _receive(self, journal_id: int, segment_index: int):
        if type(journal_id) is not int or journal_id <= 0 or type(segment_index) is not int or segment_index < 0:
            raise FileTransferError('positive journal_id and nonnegative segment_index required')
        if journal_id <= self.history_after:
            raise FileScopeError('attachment predates the current conversation session')
        rows = await asyncio.to_thread(
            self.journal.before, self.conversation, journal_id + 1, 1,
            after_id=self.history_after,
        )
        if not rows or rows[0].journal_id != journal_id:
            raise FileScopeError('attachment does not belong to the current conversation')
        message = rows[0].message
        segments = message.content.get('segments', [])
        if message.direction != 'inbound' or segment_index >= len(segments) or segments[segment_index].get('type') not in MEDIA:
            raise FileTransferError('select an inbound file/image/record/video segment from canonical history')
        segment = segments[segment_index]
        source = await self.resolve(segment)
        if not isinstance(source, dict):
            raise FileTransferError('QQ file reference is unavailable or expired')
        name = safe_name(source.get('file_name') or segment.get('name') or segment.get('file_name')
                         or (str(segment.get('file', '')).rsplit('/', 1)[-1] if '://' not in str(segment.get('file', '')) else '')
                         or (segment['type'] + '.bin'))
        declared = source.get('file_size', segment.get('file_size', segment.get('size')))
        # Some NapCat resource endpoints use 0/empty for unknown size. Positive
        # declarations are checked exactly; streamed bytes remain size-bounded.
        declared = int(declared) if declared not in (None, '') else None
        declared = declared if declared is not None and declared > 0 else None
        if declared is not None and declared > self.max_bytes:
            raise FileTransferError('file exceeds configured transfer size limit')
        state = {'size': 0, 'hash': hashlib.sha256(), 'host_storage': self.profile == 'safe-chat'}
        if self.profile == 'safe-chat':
            state['remaining_quota'] = max(0, self.conversation_quota - self._stored_bytes())
        with self._new_file('attachments', name) as (fd, relative):
            url = source.get('url', '')
            if isinstance(url, str) and url.startswith(('http://', 'https://')):
                # URL is resolved by the authenticated AstrBot/NapCat client,
                # never taken from tool arguments or message text. No cookies,
                # host proxy credentials, auth headers, or decompression.
                async with aiohttp.ClientSession(trust_env=False, auto_decompress=False,
                         timeout=aiohttp.ClientTimeout(total=300, sock_read=60)) as session:
                    async with session.get(url, max_redirects=3, headers={'Accept-Encoding': 'identity'}) as response:
                        response.raise_for_status()
                        if response.content_length is not None and response.content_length > self.max_bytes:
                            raise FileTransferError('file exceeds configured transfer size limit')
                        async for chunk in response.content.iter_chunked(CHUNK):
                            self._write(fd, chunk, state)
            else:
                # Same-host NapCat returns a real local cache path for media.
                # That path must originate in the API result, not a QQ field.
                path = source.get('file')
                if not path or not Path(path).is_absolute():
                    raise FileTransferError('NapCat returned no usable URL or shared local file; check file service deployment')
                source_fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                try:
                    if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                        raise FileTransferError('NapCat file is not a regular file')
                    while chunk := os.read(source_fd, CHUNK):
                        self._write(fd, chunk, state)
                        await asyncio.sleep(0)
                finally:
                    os.close(source_fd)
            if declared is not None and state['size'] != declared:
                raise FileTransferError('attachment download length does not match QQ metadata')
        return {'path': self._visible(relative), 'name': name, 'size': state['size'],
                'sha256': state['hash'].hexdigest(), 'journal_id': journal_id, 'segment_index': segment_index}

    def _visible(self, relative):
        if self.profile == 'group-sandbox':
            return '/workspace/' + relative
        if self.profile == 'safe-chat':
            return relative
        return str(self.storage_root / relative)

    async def send_file(self, path: str, name: str | None = None):
        state = {'size': 0, 'hash': hashlib.sha256(), 'host_storage': True}
        filename = safe_name(name or PurePosixPath(path).name)
        # A private host snapshot is never writable from the group namespace.
        # Native AstrBot File uses this same-host path, or its configured token
        # file service when NapCat runs remotely.
        self.spool_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        meta = self.spool_dir.lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_mode & 0o077:
            raise FileScopeError('outgoing file spool must be a private owned directory')
        source = self._open_source(path)
        try:
            temporary = tempfile.TemporaryDirectory(prefix='send-', dir=self.spool_dir)
        except BaseException:
            os.close(source)
            raise
        snapshot = Path(temporary.name) / filename
        try:
            original = os.fstat(source)
            async with self.storage_lock():
                with snapshot.open('xb') as target:
                    while chunk := os.read(source, CHUNK):
                        self._write(target.fileno(), chunk, state)
                        await asyncio.sleep(0)
            after = os.fstat(source)
            if (original.st_size, original.st_mtime_ns, original.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise FileTransferError('file changed while preparing the outgoing snapshot')
            await self.send(snapshot, filename)
        finally:
            os.close(source)
            temporary.cleanup()
        return {'submitted': True, 'platform_confirmed': False, 'name': filename,
                'size': state['size'], 'sha256': state['hash'].hexdigest()}


async def resolve_onebot_file(bot, conversation, segment):
    """Ask the existing AstrBot client for each exact attachment's resource."""
    kind = segment['type']
    identity = segment.get('file_id') or segment.get('file') or segment.get('id')
    if not isinstance(identity, str) or not identity or '://' in identity or '\x00' in identity:
        raise FileTransferError('QQ attachment has no resolvable resource ID')
    if '\\' in identity or any(part in {'.', '..'} for part in identity.split('/')):
        raise FileScopeError('attachment reference cannot be a filesystem path')
    route = {'self_id': int(conversation.bot_id)}
    if kind == 'file':
        params = {'file_id': identity, **route}
        action = 'get_private_file_url'
        if conversation.key.startswith('group:'):
            action = 'get_group_file_url'
            params['group_id'] = int(conversation.key.split(':', 1)[1])
            if segment.get('busid') is not None:
                params['busid'] = segment['busid']
        return await bot.call_action(action, **params)
    # The get_file endpoint can also interpret arbitrary host paths. Only QQ
    # opaque IDs/filenames are permitted; slash-containing group IDs go through
    # the scoped file URL endpoint above.
    if '/' in identity or '\\' in identity or re.match(r'^[A-Za-z]:', identity):
        raise FileTransferError('media reference must be a QQ resource identifier')
    action = 'get_image' if kind == 'image' else 'get_file'
    parameter = {'file': identity} if kind == 'image' else {'file_id': identity}
    return await bot.call_action(action, **parameter, **route)

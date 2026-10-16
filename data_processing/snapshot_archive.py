"""Lossless, content-addressed snapshot history with compressed shared chunks."""

import contextlib
import hashlib
import io
import os
import sqlite3
import zlib
from pathlib import Path, PurePosixPath

CHUNK_SIZE = 64 * 1024
ARCHIVE = Path('output/warehouse/history.sqlite3')


def safe_member(root, name):
    name = str(name).replace('\\', '/')
    relative = PurePosixPath(name)
    if not relative.parts or relative.is_absolute() or '..' in relative.parts or ':' in name:
        raise ValueError('Invalid archive member: ' + name)
    root = Path(root).resolve()
    target = (root / name).resolve()
    if target == root or root not in target.parents:
        raise ValueError('Archive member outside project: ' + name)
    return target


class SnapshotArchive:
    def __init__(self, root, readonly=False):
        self.root = Path(root).resolve()
        self.path = self.root / ARCHIVE
        self.readonly = readonly
        if readonly:
            self.db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=60)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path, timeout=60)
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS chunks (
                    digest BLOB PRIMARY KEY, size INTEGER NOT NULL, content BLOB NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS objects (
                    digest TEXT PRIMARY KEY, size INTEGER NOT NULL, chunks BLOB NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS members (
                    path TEXT PRIMARY KEY, digest TEXT NOT NULL REFERENCES objects(digest)
                ) WITHOUT ROWID;
            ''')
        self.known = {r[0] for r in self.db.execute('SELECT digest FROM chunks')}
        self.verified = set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def put_stream(self, stream, member=None, expected=None):
        if self.readonly:
            raise ValueError('Read-only snapshot archive')
        if member is not None:
            safe_member(self.root, member)
        digest, size, parts, inserted = hashlib.sha256(), 0, [], set()
        try:
            with self.db:
                while block := stream.read(CHUNK_SIZE):
                    digest.update(block)
                    size += len(block)
                    chunk = hashlib.sha256(block).digest()
                    parts.append(chunk)
                    if chunk not in self.known:
                        self.db.execute('INSERT OR IGNORE INTO chunks VALUES (?, ?, ?)',
                                        (chunk, len(block), zlib.compress(block, 6)))
                        self.known.add(chunk)
                        inserted.add(chunk)
                sha = digest.hexdigest()
                if expected is not None and sha != expected:
                    raise ValueError('Snapshot hash mismatch: ' + str(member))
                self.db.execute('INSERT OR IGNORE INTO objects VALUES (?, ?, ?)', (sha, size, b''.join(parts)))
                if member is not None:
                    self.db.execute('INSERT OR REPLACE INTO members VALUES (?, ?)', (str(member).replace('\\', '/'), sha))
        except Exception:
            self.known.difference_update(inserted)
            raise
        self.verify(sha)
        return {'sha256': sha, 'bytes': size}

    def put_bytes(self, content, member=None):
        return self.put_stream(io.BytesIO(content), member)

    def put_file(self, path):
        path = Path(path).resolve()
        if self.root not in path.parents:
            raise ValueError('Snapshot file outside project')
        before = path.stat()
        with path.open('rb') as stream:
            item = self.put_stream(stream, path.relative_to(self.root).as_posix())
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('Snapshot changed while archiving: ' + str(path))
        return item

    def blocks(self, sha):
        row = self.db.execute('SELECT size, chunks FROM objects WHERE digest=?', (sha,)).fetchone()
        if row is None:
            raise FileNotFoundError('Snapshot not archived: ' + sha)
        size, parts = row
        if len(parts) % 32:
            raise ValueError('Invalid snapshot chunk index')
        total, digest = 0, hashlib.sha256()
        for index in range(0, len(parts), 32):
            key = parts[index:index + 32]
            chunk = self.db.execute('SELECT size, content FROM chunks WHERE digest=?', (key,)).fetchone()
            if chunk is None:
                raise ValueError('Missing snapshot chunk')
            block = zlib.decompress(chunk[1])
            if len(block) != chunk[0] or hashlib.sha256(block).digest() != key:
                raise ValueError('Corrupt snapshot chunk')
            total += len(block)
            digest.update(block)
            yield block
        if total != size or digest.hexdigest() != sha:
            raise ValueError('Corrupt snapshot object')

    def verify(self, sha):
        if sha not in self.verified:
            for _ in self.blocks(sha):
                pass
            self.verified.add(sha)

    def read_bytes(self, sha):
        return b''.join(self.blocks(sha))

    def restore(self, member):
        target = safe_member(self.root, member)
        row = self.db.execute('SELECT digest FROM members WHERE path=?', (str(member).replace('\\', '/'),)).fetchone()
        if row is None:
            raise FileNotFoundError('Unknown archive member: ' + str(member))
        sha = row[0]
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                raise ValueError('Refusing to overwrite a newer file: ' + str(member))
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.restore.tmp')
        try:
            with temporary.open('wb') as stream:
                for block in self.blocks(sha):
                    stream.write(block)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def merge(self, other_root):
        """Merge an isolated worker's committed history before its workspace is removed."""
        with SnapshotArchive(other_root, readonly=True) as other:
            for sha, in other.db.execute('SELECT digest FROM objects'):
                self.put_bytes(other.read_bytes(sha))
            with self.db:
                for member, sha in other.db.execute('SELECT path, digest FROM members'):
                    safe_member(self.root, member)
                    self.db.execute('INSERT OR IGNORE INTO members VALUES (?, ?)', (member, sha))


def read_snapshot(ref, root):
    """Read legacy loose snapshots and packed history, checking their original hash."""
    path = safe_member(root, ref['path'])
    if path.is_file():
        content = path.read_bytes()
    else:
        with SnapshotArchive(root, readonly=True) as archive:
            content = archive.read_bytes(ref['sha256'])
    if hashlib.sha256(content).hexdigest() != ref['sha256']:
        raise ValueError('Snapshot evidence hash mismatch')
    return content

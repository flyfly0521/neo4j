"""Client for the compile-only C++ engine (JSON lines over pipes, protocol 2)."""
import itertools
import json
import os
import subprocess
import threading
from pathlib import Path

_ENGINE_DIR = Path(__file__).resolve().parents[1] / 'engine'


def resolve_binary(name, env_var):
    """Explicit override, then patched builds before stock builds.

    Within each variant, prefer engine/build over the migrated engine/bin.
    Keep the fixes enabled even if only a stock local build is available.
    """
    if os.environ.get(env_var):
        return Path(os.environ[env_var]).expanduser().resolve()
    for variant in (name + '_patched', name):
        for folder in ('build', 'bin'):
            path = _ENGINE_DIR / folder / variant
            if path.is_file() and os.access(path, os.X_OK):
                return path
    return _ENGINE_DIR / 'build' / name


DEFAULT_ENGINE = resolve_binary('incgq_rewrite_engine', 'CICO_ENGINE')


def _limit_memory():
    """Cap the engine's address space (CICO_ENGINE_MEMORY_GB, default 8; 0 =
    unlimited).  A compile normally needs tens of MB, but some rewrite
    searches in NeuG can grow without bound (a Neo4j bi-17 translation
    reached 105 GB on a shared machine); with the cap the allocation fails
    inside the request instead of exhausting the host."""
    import resource
    gb = float(os.environ.get('CICO_ENGINE_MEMORY_GB', '8'))
    if gb > 0:
        limit = int(gb * (1 << 30))
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


class EngineError(RuntimeError):
    def __init__(self, response):
        self.response = response
        super().__init__(f"{response.get('stage', 'engine')}: {response.get('error', response)}")

    @property
    def stage(self):
        return self.response.get('stage', 'engine')


class Engine:
    """One engine process owns one schema + FILL catalog.  Requests are
    serialised by a lock, so one instance may be shared between threads."""

    def __init__(self, binary=None, log_path=None):
        binary = (Path(binary).expanduser().resolve() if binary else
                  resolve_binary('incgq_rewrite_engine', 'CICO_ENGINE'))
        if not binary.is_file():
            raise FileNotFoundError(f'Engine binary not found: {binary} (build middleware/engine first)')
        self._log = open(log_path, 'ab') if log_path else subprocess.DEVNULL
        self._process = subprocess.Popen([str(binary), '--compile-only'], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=self._log, text=True, bufsize=1,
                                         preexec_fn=_limit_memory)
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.startup = self._read()
        if self.startup.get('event') != 'ready':
            self.close()
            raise EngineError(self.startup)

    def alive(self):
        return not getattr(self, '_eof', False) and self._process.poll() is None

    def _read(self):
        line = self._process.stdout.readline()
        if not line:
            self._eof = True
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
            raise EngineError({'stage': 'engine', 'error': f'engine exited ({self._process.poll()})'})
        return json.loads(line)

    def request(self, op, **fields):
        with self._lock:
            identity = next(self._ids)
            self._process.stdin.write(json.dumps(dict(op=op, rid=identity, **fields)) + '\n')
            self._process.stdin.flush()
            response = self._read()
        if response.get('rid') != identity:
            raise EngineError({'stage': 'protocol', 'error': f'response id mismatch: {response}'})
        if not response.get('ok'):
            raise EngineError(response)
        return response

    def load(self, schema_yaml, stats=None):
        return self.request('load', schema=schema_yaml, **({'stats': stats} if stats else {}))

    def register(self, index_id, fills):
        return self.request('register', id=index_id, fills=list(fills))

    def drop(self, index_id):
        return self.request('drop', candidate=index_id)

    def indexes(self):
        return self.request('indexes')

    def rewrite(self, cypher, parameter_types=None):
        return self.request('rewrite', cypher=cypher, parameter_types=parameter_types or {})

    def bind(self, cypher, parameter_types=None, catalog='baseline'):
        return self.request('bind', cypher=cypher, parameter_types=parameter_types or {}, catalog=catalog)

    def close(self):
        if self._process.poll() is None:
            self._process.stdin.close()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._process.stdout and not self._process.stdout.closed:
            self._process.stdout.close()
        if self._log is not subprocess.DEVNULL:
            self._log.close()

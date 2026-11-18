"""Apply a bundled patch to the current NeuG source without changing its tree.

Never restore a historical whole-file snapshot: doing so hides fixes already
present in the linked kernel. Context mismatches fail the build instead.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--neug-source', required=True, type=Path)
    parser.add_argument('--patch', required=True, type=Path, action='append')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--source', default='src/compiler/binder/rewriter/rewriter_v4.cpp')
    args = parser.parse_args()
    source = args.source
    base = (args.neug_source / source).read_bytes()
    before = hashlib.sha1(f'blob {len(base)}\0'.encode() + base).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cico-patch-') as temp:
        staged = Path(temp) / Path(source).name
        staged.write_bytes(base)
        for patch in args.patch:
            sections = patch.read_text().split('diff --git ')
            section, = [s for s in sections if s.startswith(f'a/{source} b/{source}\n')]
            subprocess.run(['patch', '--batch', '--forward', '--fuzz=0', str(staged)],
                           input=('diff --git ' + section).encode(), check=True)
        patched = staged.read_bytes()
    digest = hashlib.sha1(f'blob {len(patched)}\0'.encode() + patched).hexdigest()
    if not args.output.exists() or args.output.read_bytes() != patched:
        args.output.write_bytes(patched)
    args.output.with_suffix('.json').write_text(json.dumps({
        'source': source, 'base_blob': before, 'patched_blob': digest,
        'base_sha256': hashlib.sha256(base).hexdigest(),
        'patches': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in args.patch},
    }, indent=2) + '\n')


if __name__ == '__main__':
    main()

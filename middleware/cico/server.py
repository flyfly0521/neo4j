"""Language-neutral HTTP/JSON interface (standard library only).

  POST /statement  {"cypher": "...", "parameters": {...}?, "parameter_types": {...}?, "index_id": "..."?}
                   -> Result (FILL / DDL are applied to the middleware; queries are rewritten)
  POST /script     {"script": "stmt; stmt; ..."}         -> {"results": [Result, ...]}
  POST /index      {"id": "...", "fills": ["FILL ...", ...]} -> Result
  DELETE /index/<id>                                      -> {"dropped": n}
  GET  /indexes                                           -> registered indexes
  GET  /schema                                            -> middleware schema (YAML text)
  GET  /build                                             -> target statements materializing the indexes
  GET  /health
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote


def make_handler(middleware, catalog=None, index_executor=None):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self):
            length = int(self.headers.get('Content-Length') or 0)
            return json.loads(self.rfile.read(length) or b'{}')

        def log_message(self, fmt, *args):  # quiet by default
            pass

        def do_GET(self):
            if self.path == '/health':
                return self._send(200, {'ok': True, 'profile': middleware.profile.name})
            if self.path == '/indexes':
                return self._send(200, middleware.indexes())
            if self.path == '/schema':
                return self._send(200, {'schema': middleware.schema.to_yaml()})
            if self.path == '/build':
                return self._send(200, {'indexes': middleware.build_statements()})
            if self.path == '/index-files' and catalog is not None:
                return self._send(200, catalog.last_report)
            self._send(404, {'error': 'not found'})

        def do_POST(self):
            try:
                body = self._json()
                if self.path == '/index-files/reload' and catalog is not None:
                    return self._send(200, catalog.reload(index_executor))
                if catalog is not None and self.path in ('/index', '/index/neo4j', '/script'):
                    raise ValueError('Managed catalog: edit the configured FILL files and reload')
                if self.path == '/statement':
                    if catalog is not None:
                        from .statements import classify
                        if classify(body['cypher']) in ('fill', 'ddl'):
                            raise ValueError('Managed catalog: edit the configured FILL files and reload')
                    result = middleware.execute(body['cypher'], body.get('parameters'),
                                                body.get('parameter_types'), body.get('index_id'))
                    return self._send(200, result.to_dict())
                if self.path == '/script':
                    return self._send(200, {'results': [r.to_dict() for r in middleware.execute_script(body['script'])]})
                if self.path == '/index/neo4j':
                    results = middleware.register_neo4j_index(body['script'], body.get('id'), body.get('parameters'))
                    return self._send(200, {'results': [r.to_dict() for r in results]})
                if self.path == '/index':
                    result = middleware.register_index(body['fills'], body.get('id'),
                                                       input_dialect=body.get('input_dialect'))
                    return self._send(200, result.to_dict())
                self._send(404, {'error': 'not found'})
            except Exception as error:  # report, never crash the server
                self._send(400, {'ok': False, 'error': f'{type(error).__name__}: {error}'})

        def do_DELETE(self):
            if catalog is not None and self.path.startswith('/index/'):
                return self._send(400, {'error': 'Managed catalog: edit the configured FILL files and reload'})
            if self.path.startswith('/index/'):
                return self._send(200, {'dropped': middleware.drop_index(unquote(self.path[len('/index/'):]))})
            self._send(404, {'error': 'not found'})

    return Handler


def serve(middleware, host='127.0.0.1', port=8765, catalog=None, index_executor=None):
    server = ThreadingHTTPServer((host, port), make_handler(middleware, catalog, index_executor))
    try:
        server.serve_forever()
    finally:
        server.server_close()

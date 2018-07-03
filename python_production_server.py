import flask
import sys
import inspect
import uuid
import numpy as np
import collections
import threading

_archives = {}
_type_map = {
    str: 'char',
    float: 'double',
    int: 'int32',
    bool: 'logical',
    'float64': 'double',
    'int8': 'int8',
    'int16': 'int16',
    'int32': 'int32',
    'int64': 'int64',
    'uint8': 'uint8',
    'uint16': 'uint16',
    'uint32': 'uint32',
    'uint64': 'uint64',
    np.int64: 'int64',
    np.int32: 'int32',
    np.int16: 'int16',
    np.int8: 'int8',
    np.uint64: 'uint64',
    np.uint32: 'uint32',
    np.uint16: 'uint16',
    np.uint8: 'uint8',
    np.bool_: 'logical',
    np.float64: 'double',
    np.float32: 'single'
}

_reverse_type_map = {
    'char': str,
    'double': np.double,
    'single': np.float32,
    'int8': np.int8,
    'int16': np.int16,
    'int32': np.int32,
    'int64': np.int64,
    'uint8': np.uint8,
    'uint16': np.uint16,
    'uint32': np.uint32,
    'uint64': np.uint64,
    'logical': np.bool_,
}

_app = flask.Flask(__name__)
_server_seq = 0
_async_requests = collections.defaultdict(list)


def _execute_function(func, params, n_arg_out=-1, output_format=None):
    inf_format = 'string'
    nan_format = 'string'
    output_mode = 'small'
    if output_format:
        if 'nanInfFormat' in output_format:
            nan_format = inf_format = output_format['nanInfFormat']
        if 'mode' in output_format:
            output_mode = output_format['mode']

    for i, par_name in enumerate(list(inspect.signature(func).parameters)):
        params[i] = func.__annotations__[par_name](params[i])

    result = list(_iterify(func(*params)))
    if n_arg_out != -1:
        result = result[:n_arg_out]

    if output_mode == 'large':
        annotations = _iterify(func.__annotations__['return'])
        for i, out in enumerate(result):
            typ, size = _evaluate_type(annotations[i])
            if type(out) == np.ndarray:
                size = out.shape
            else:
                # Try to set length based on element length (for strings and lists)
                try:
                    size = (1, len(out))
                except TypeError:
                    # Element has no length - use default (1, 1) size
                    pass

            result[i] = {
                'mwtype': typ,
                'mwsize': size,
                'mwdata': list(_iterify(out))
            }
    else:
        result = list(map(lambda x: list(_iterify(x)), result))

    return result


class _AsyncFunctionCall:

    def __init__(self, func, rhs, n_arg_out=-1, output_format=None, client_id=None, collection=None):
        self.id = uuid.uuid4().hex
        self.collection = collection if collection else uuid.uuid4().hex
        self.func = func
        self.rhs = rhs
        self.n_arg_out = n_arg_out
        self.output_format = output_format
        self.client_id = client_id if client_id else ''

        self.state = 'READING'
        self.result = []
        self.last_modified_seq = _server_seq
    
    def execute(self):
        global _server_seq
        self.state = 'PROCESSING'
        _server_seq += 1
        self.last_modified_seq = _server_seq
        try:
            self.result = _execute_function(self.func, self.rhs, self.n_arg_out, self.output_format)
            _server_seq += 1
            self.last_modified_seq = _server_seq
            self.state = 'READY'
        except Exception:
            _server_seq += 1
            self.last_modified_seq = _server_seq
            self.state = 'ERROR'

    def cancel(self):
        global _server_seq
        _server_seq += 1
        self.last_modified_seq = _server_seq
        self.state = 'CANCELLED'
    
    def status_dict(self):
        return {
            'id': self.id,
            'self': '/~' + self.collection + '/requests/' + self.id,
            'up': '/~' + self.collection + '/requests',
            'lastModifiedSeq': self.last_modified_seq,
            'state': self.state,
            'client': self.client_id
        }


def _iterify(x):
    if isinstance(x, collections.Sequence) and type(x) != str:
        return x
    return (x,)


def register_function(archive, func):
    global _server_seq
    _server_seq += 1
    if archive not in _archives:
        _archives[archive] = {
            'uuid': archive[:12] + '_' + uuid.uuid4().hex,
            'functions': {}
        }
    _archives[archive]['functions'][func.__name__] = func


def _evaluate_type(annotation):
    if type(annotation) == np.ndarray:
        typ = _type_map[annotation.dtype.__str__()]
        size = annotation.shape
    else:
        typ = _type_map[annotation]
        size = [1, 1]

    if typ == 'char':
        size = [1, 'X']
    return typ, size


@_app.route('/api/discovery', methods=['GET'])
def _discovery():
    response = {
        'discoverySchemaVersion': '1.0.0',
        'archives': {}
    }

    vi = sys.version_info
    py_version = str(vi[0]) + '.' + str(vi[1]) + '.' + str(vi[2])
    for archive_key, archive in _archives.items():
        arch_response = {
            'archiveSchemaVersion': '1.0.0',
            'archiveUuid': archive['uuid'],
            'functions': {},
            'matlabRuntimeVersion': py_version
        }

        for func_name, func in archive['functions'].items():
            assert len(func.__annotations__), 'All functions must be annotated!'
            assert 'return' in func.__annotations__, 'Return type must be annotated!'

            arch_response['functions'][func_name] = {
                'signatures': [{
                    'help': func.__doc__,
                    'inputs': [],
                    'outputs': []
                }]
            }

            for i, output in enumerate(_iterify(func.__annotations__['return'])):
                typ, size = _evaluate_type(output)
                arch_response['functions'][func.__name__]['signatures'][0]['outputs'].append({
                    'mwsize': size,
                    'mwtype': typ,
                    'name': 'out' + str(i+1)
                })

            for par_name in list(inspect.signature(func).parameters):
                typ, size = _evaluate_type(func.__annotations__[par_name])
                arch_response['functions'][func.__name__]['signatures'][0]['inputs'].append({
                    'mwsize': size,
                    'mwtype': typ,
                    'name': par_name
                })

        response['archives'][archive_key] = arch_response

    return flask.jsonify(response)


def _sync_request(archive_name, function_name, request_body):
    global _server_seq
    _server_seq += 1
    params = request_body['rhs']
    n_arg_out = request_body['nargout'] if 'nargout' in request_body else -1
    output_format = request_body['outputFormat'] if 'outputFormat' in request_body else None

    func = _archives[archive_name]['functions'][function_name]
    result = _execute_function(func, params, n_arg_out, output_format)

    return flask.jsonify({'lhs': result})


def _async_request(archive_name, function_name, request_body, client_id=None):
    global _server_seq
    _server_seq += 1
    func = _archives[archive_name]['functions'][function_name]
    params = request_body['rhs']
    n_arg_out = request_body['nargout'] if 'nargout' in request_body else -1
    output_format = request_body['outputFormat'] if 'outputFormat' in request_body else None

    async_call = _AsyncFunctionCall(func, params, n_arg_out, output_format, client_id)
    _async_requests[async_call.collection].append(async_call)

    response = async_call.status_dict()

    thread = threading.Thread(target=async_call.execute)
    thread.start()

    print(_async_requests)

    return flask.jsonify(response), 201


@_app.route('/<archive_name>/<function_name>', methods=['POST'])
def _call_request(archive_name, function_name):
    mode = flask.request.args.get('mode', False)
    if mode and mode == 'async':
        client_id = flask.request.args.get('client', None)
        return _async_request(archive_name, function_name, flask.json.loads(flask.request.data), client_id)
    else:
        return _sync_request(archive_name, function_name, flask.json.loads(flask.request.data))


@_app.route('/<collection_id>', methods=['GET'])
def _get_collection(collection_id):
    since = flask.request.args.get('since', None)
    if not since:
        return '400 MissingParamSince', 400
    clients = flask.request.args.get('clients', None)
    clients = clients.split(',') if clients else None
    ids = flask.request.args.get('ids', None)
    ids = ids.split(',') if ids else None
    if not clients and not ids:
        return '400 MissingQueryParams', 400

    try:
        response = {
            'createdSeq': _server_seq,
            'data': []
        }

        for request in _async_requests[collection_id]:
            if ids and request.id in ids or clients and request.client_id in clients:
                response['data'].append(request.status_dict())

        return flask.jsonify(response)
    except KeyError:
        return '', 404


def run(ip='0.0.0.0', port='8080'):
    _app.run(ip, port)

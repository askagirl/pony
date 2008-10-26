import re, threading, inspect, warnings, urllib

from itertools import izip
from operator import itemgetter

from pony.httputils import split_url
from pony.autoreload import on_reload

class NodefaultType(object):
    def __repr__(self): return '__nodefault__'
    
__nodefault__ = NodefaultType()

registry_lock = threading.RLock()
registry = ({}, [], [])
system_routes = []

class Route(object):
    def __init__(self, func, url, host, port, redirect, headers):
        self.func = func
        if not hasattr(func, 'argspec'):
            func.argspec = self.getargspec(func)
            func.dummy_func = self.create_dummy_func(func)
        self.url = url
        if host is not None:
            if not isinstance(host, basestring): raise TypeError('Host must be string')
            if ':' in host:
                if port is not None: raise TypeError('Duplicate port specification')
                host, port = host.split(':')
        self.host, self.port = host, port and int(port) or None
        self.path, self.qlist = split_url(url, strict_parsing=True)
        self.redirect = redirect
        module = func.__module__
        self.system = module.startswith('pony.') and not module.startswith('pony.examples.')
        self.headers = headers
        self.args = set()
        self.keyargs = set()
        self.parsed_path = []
        self.star = False
        for component in self.path:
            if self.star: raise TypeError("'$*' must be last element in url path")
            elif component != '$*': self.parsed_path.append(self.parse_component(component))
            else: self.star = True
        self.parsed_query = []
        for name, value in self.qlist:
            if value == '$*': raise TypeError("'$*' does not allowed in query part of url")
            is_param, x = self.parse_component(value)
            self.parsed_query.append((name, is_param, x))
        self.check()
        if self.system: system_routes.append(self)
        self.register()
    @staticmethod
    def getargspec(func):
        original_func = getattr(func, 'original_func', func)
        names, argsname, keyargsname, defaults = inspect.getargspec(original_func)
        defaults = defaults and list(defaults) or []
        diff = len(names) - len(defaults)
        converters = {}
        try:
            for i, value in enumerate(defaults):
                if value is None: continue
                elif isinstance(value, basestring):
                    defaults[i] = unicode(value).encode('utf8')
                elif callable(value):
                    converters[diff+i] = value
                    defaults[i] = __nodefault__
                else: converters[diff+i] = value.__class__
        except UnicodeDecodeError: raise ValueError(
            'Default value contains non-ascii symbols. Such default values must be in unicode.')
        return names, argsname, keyargsname, defaults, converters
    @staticmethod
    def create_dummy_func(func):
        spec = inspect.formatargspec(*func.argspec[:-1])[1:-1]
        source = "lambda %s: __locals__()" % spec
        return eval(source, dict(__locals__=locals, __nodefault__=__nodefault__))
    component_re = re.compile(r"""
            [$]
            (?: (\d+)              # param number (group 1)
            |   ([A-Za-z_]\w*)     # param identifier (group 2)
            )
        |   (                      # path component (group 3)
                (?:[$][$] | [^$])+
            )
        """, re.VERBOSE)
    def parse_component(self, component):
        items = list(self.split_component(component))
        if not items: return False, ''
        if len(items) == 1: return items[0]
        pattern = []
        regexp = []
        for i, item in enumerate(items):
            if item[0]:
                pattern.append('/')
                try: nextchar = items[i+1][1][0]
                except IndexError: regexp.append('(.*)$')
                else: regexp.append('([^%s]*)' % nextchar.replace('\\', '\\\\'))
            else:
                s = item[1]
                pattern.append(s)
                for char in s:
                    regexp.append('[%s]' % char.replace('\\', '\\\\'))
        pattern = ''.join(pattern)
        regexp = ''.join(regexp)
        return True, [ pattern, re.compile(regexp) ] + items
    def split_component(self, component):
        pos = 0
        is_param = False
        for match in self.component_re.finditer(component):
            if match.start() != pos:
                raise ValueError('Invalid url component: %r' % component)
            i = match.lastindex
            if 1 <= i <= 2:
                if is_param: raise ValueError('Invalid url component: %r' % component)
                is_param = True
                if i == 1: yield is_param, self.adjust(int(match.group(i)) - 1)
                elif i == 2: yield is_param, self.adjust(match.group(i))
            elif i == 3:
                is_param = False
                yield is_param, match.group(i).replace('$$', '$')
            else: assert False
            pos = match.end()
        if pos != len(component):
            raise ValueError('Invalid url component: %r' % component)
    def adjust(self, x):
        names, argsname, keyargsname, defaults, converters = self.func.argspec
        args, keyargs = self.args, self.keyargs
        if isinstance(x, int):
            if x < 0 or x >= len(names) and argsname is None: raise TypeError('Invalid parameter index: %d' % (x+1))
            if x in args: raise TypeError('Parameter index %d already in use' % (x+1))
            args.add(x)
            return x
        elif isinstance(x, basestring):
            try: i = names.index(x)
            except ValueError:
                if keyargsname is None or x in keyargs: raise TypeError('Unknown parameter name: %s' % x)
                keyargs.add(x)
                return x
            else:
                if i in args: raise TypeError('Parameter name %s already in use' % x)
                args.add(i)
                return i
        assert False
    def check(self):
        names, argsname, keyargsname, defaults, converters = self.func.argspec
        if self.star and not argsname: raise TypeError(
            "Function %s does not accept arbitrary argument list" % self.func.__name__)
        args, keyargs = self.args, self.keyargs
        diff = len(names) - len(defaults)
        for i, name in enumerate(names[:diff]):
            if i not in args: raise TypeError('Undefined path parameter: %s' % name)
        for i, name, default in izip(xrange(diff, diff+len(defaults)), names[diff:], defaults):
            if default is __nodefault__ and i not in args:
                raise TypeError('Undefined path parameter: %s' % name)
        if args:
            for i in range(len(names), max(args)):
                if i not in args: raise TypeError('Undefined path parameter: %d' % (i+1))
    def register(self):
        def get_url_map(route):
            result = {}
            for i, (is_param, x) in enumerate(route.parsed_path):
                if is_param: result[i] = isinstance(x, list) and x[0] or '/'
                else: result[i] = ''
            for name, is_param, x in route.parsed_query:
                if is_param: result[name] = isinstance(x, list) and x[0] or '/'
                else: result[name] = ''
            if route.star: result['$*'] = len(route.parsed_path)
            if route.host: result[('host',)] = route.host
            if route.port: result[('port',)] = route.port
            return result
        url_map = get_url_map(self)
        qdict = dict(self.qlist)
        registry_lock.acquire()
        try:
            for route, _, _ in get_routes(self.path, qdict, self.host, self.port):
                if url_map == get_url_map(route):
                    warnings.warn('Url path already in use (old route was removed): %s' % route.url)
                    _remove(route)
            d, list1, list2 = registry
            for is_param, x in self.parsed_path:
                if is_param: d, list1, list2 = d.setdefault(None, ({}, [], []))
                else: d, list1, list2 = d.setdefault(x, ({}, [], []))
            if not self.star: self.list = list1
            else: self.list = list2
            self.func.__dict__.setdefault('routes', []).insert(0, self)
            self.list.insert(0, self)
        finally: registry_lock.release()

def get_routes(path, qdict, host, port):
    # registry_lock.acquire()
    # try:
    variants = [ registry ]
    routes = []
    for i, component in enumerate(path):
        new_variants = []
        for d, list1, list2 in variants:
            variant = d.get(component)
            if variant: new_variants.append(variant)
            # if component:
            variant = d.get(None)
            if variant: new_variants.append(variant)
            routes.extend(list2)
        variants = new_variants
    for d, list1, list2 in variants: routes.extend(list1)
    # finally: registry_lock.release()

    result = []
    not_found = object()
    for route in routes:
        args, keyargs = {}, {}
        priority = 0
        if route.host is not None:
            if route.host != host: continue
            priority += 10000
        if route.port is not None:
            if route.port != port: continue
            priority += 100
        for i, (is_param, x) in enumerate(route.parsed_path):
            if not is_param:
                priority += 1
                continue
            value = path[i].decode('utf8')
            if isinstance(x, int): args[x] = value
            elif isinstance(x, basestring): keyargs[x] = value
            elif isinstance(x, list):
                match = x[1].match(value)
                if not match: break
                params = [ y for is_param, y in x[2:] if is_param ]
                groups = match.groups()
                n = len(x) - len(params)
                if not x[-1][0]: n += 1
                priority += n
                assert len(params) == len(groups)
                for param, value in zip(params, groups):
                    if isinstance(param, int): args[param] = value
                    elif isinstance(param, basestring): keyargs[param] = value
                    else: assert False
            else: assert False
        else:
            names, _, _, defaults, converters = route.func.argspec
            diff = len(names) - len(defaults)
            non_used_query_params = set(qdict)
            for name, is_param, x in route.parsed_query:
                non_used_query_params.discard(name)
                value = qdict.get(name, not_found)
                if value is not not_found: value = value.decode('utf8')
                if not is_param:
                    if value != x: break
                    priority += 1
                elif isinstance(x, int):
                    if value is not_found:
                        if diff <= x < len(names): continue
                        else: break
                    else: args[x] = value
                elif isinstance(x, basestring):
                    if value is not_found: break
                    keyargs[x] = value
                elif isinstance(x, list):
                    if value is not_found:
                        for is_param, y in x[2:]:
                            if not is_param: continue
                            if isinstance(y, int) and diff <= y < len(names): continue
                            break
                        else: continue
                        break
                    match = x[1].match(value)
                    if not match: break
                    params = [ y for is_param, y in x[2:] if is_param ]
                    groups = match.groups()
                    n = len(x) - len(params) - 2
                    if not x[-1][0]: n += 1
                    priority += n
                    assert len(params) == len(groups)
                    for param, value in zip(params, groups):
                        if isinstance(param, int): args[param] = value
                        elif isinstance(param, basestring):
                            keyargs[param] = value
                        else: assert False
                else: assert False
            else:
                arglist = [ None ] * len(names)
                arglist[diff:] = defaults
                for i, value in sorted(args.items()):
                    converter = converters.get(i)
                    if converter is not None:
                        try: value = converter(value)
                        except: break
                    try: arglist[i] = value
                    except IndexError:
                        assert i == len(arglist)
                        arglist.append(value)
                else:
                    if __nodefault__ in arglist[diff:]: continue
                    if len(route.parsed_path) != len(path):
                        assert route.star
                        arglist.extend(path[len(route.parsed_path):])
                    result.append((route, arglist, keyargs, priority, len(non_used_query_params)))
    if result:
        x = max(map(itemgetter(3), result))
        result = [ tup for tup in result if tup[3] == x ]
        x = min(map(itemgetter(4), result))
        result = [ tup[:3] for tup in result if tup[4] == x ]
    return result

class PathError(Exception): pass

def build_url(route, keyparams, indexparams, host, port, script_name):
    names, argsname, keyargsname, defaults, converters = route.func.argspec
    path = []
    used_indexparams = set()
    used_keyparams = set()
    diff = len(names) - len(defaults)
    def build_param(x):
        if isinstance(x, int):
            value = indexparams[x]
            used_indexparams.add(x)
            is_default = False
            if diff <= x < len(names):
                if value is __nodefault__: raise PathError('Value for paremeter %r does not set' % names[x])
                default = defaults[x-diff]
                if value is None and default is None or value == unicode(default).encode('utf8'):
                    is_default = True
            return is_default, value
        elif isinstance(x, basestring):
            try: value = keyparams[x]
            except KeyError: assert False, 'Parameter not found: %r' % x
            used_keyparams.add(x)
            return False, value
        elif isinstance(x, list):
            result = []
            is_default = True
            for is_param, y in x[2:]:
                if not is_param: result.append(y)
                else:
                    is_default_2, component = build_param(y)
                    is_default = is_default and is_default_2
                    if component is None: raise PathError('Value for parameter %r is None' % y)
                    result.append(component)
            return is_default, ''.join(result)
        else: assert False

    for is_param, x in route.parsed_path:
        if not is_param: component = x
        else:
            is_default, component = build_param(x)
            if component is None: raise PathError('Value for parameter %r is None' % x)
        path.append(urllib.quote(component, safe=':@&=+$,'))
    if route.star:
        for i in range(len(route.args), len(indexparams)):
            path.append(urllib.quote(indexparams[i], safe=':@&=+$,'))
            used_indexparams.add(i)
    p = '/'.join(path)

    qlist = []
    for name, is_param, x in route.parsed_query:
        if not is_param: qlist.append((name, x))
        else:
            is_default, value = build_param(x)
            if not is_default:
                if value is None: raise PathError('Value for parameter %r is None' % x)
                qlist.append((name, value))
    quote_plus = urllib.quote_plus
    q = "&".join(("%s=%s" % (quote_plus(name), quote_plus(value))) for name, value in qlist)

    errmsg = 'Not all parameters were used during path construction'
    if len(used_keyparams) != len(keyparams): raise PathError(errmsg)
    if len(used_indexparams) != len(indexparams):
        for i, value in enumerate(indexparams):
            if i not in used_indexparams and value != defaults[i-diff]: raise PathError(errmsg)

    url = q and '?'.join((p, q)) or p
    result = '/'.join((script_name, url))
    if route.host is None or route.host == host:
        if route.port is None or route.port == port: return result
    host = route.host or host
    port = route.port or 80
    if port == 80: return 'http://%s%s' % (host, result)
    return 'http://%s:%d%s' % (host, port, result)

def remove(x, host=None, port=None):
    if isinstance(x, basestring):
        path, qlist = split_url(x, strict_parsing=True)
        qdict = dict(qlist)
        registry_lock.acquire()
        try:
            for route, _, _ in get_routes(path, qdict, host, port): _remove(route)
        finally: registry_lock.release()
    elif hasattr(x, 'routes'):
        assert host is None and port is None
        registry_lock.acquire()
        try:
            for route in list(x.routes): _remove(route)
        finally: registry_lock.release()
    else: raise ValueError('This object is not bound to url: %r' % x)

def _remove(route):
    url_cache.clear()
    route.list.remove(route)
    route.func.routes.remove(route)
            
@on_reload
def clear():
    registry_lock.acquire()
    try:
        _clear(*registry)
        for route in system_routes: route.register()
    finally: registry_lock.release()

def _clear(dict, list1, list2):
    url_cache.clear()
    for route in list1: route.func.routes.remove(route)
    list1[:] = []
    for route in list2: route.func.routes.remove(route)
    list2[:] = []
    for inner_dict, list1, list2 in dict.itervalues():
        _clear(inner_dict, list1, list2)
    dict.clear()
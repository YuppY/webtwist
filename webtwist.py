# encoding: utf-8

"""
    Lightweight library for easy building of twisted-based web applications

    It focuses these issues of twisted API:

    1. Resources and routing with child references are not easy. webtwist maps
       functions directly to paths via webtwist.route:

           @webtwist.route('GET /hello/{name}')
           def say_hello(request, name):
               return 'Hello, %s!' % name

    2. request.args are lists of arg values, this implementation often lead to
       errors.

       webtwist.Query is object with dict API. You can get first value of arg
       by query[arg_name] and list of all arg values by query.values(arg_name)

    3. inlineCallbacks are too complex: you have to decorate each generator
       turning it into deferred and to call obscure defer.returnValue to return.

       webtwist.Coroutine doesn't require any decorators, it simply walks all
       nested generators to find Deferred to attach. Return of value
       implemented as in PEP 380 (it is return statement in Python 3.3 or raise
       StopIteration(value) in earlier versions).

       Router automatically wraps generators into Coroutines. Coroutines are
       producers attached to request. Aborted request stops coroutine.

       inlineCallbacks had one minor side-effect: if you call decorated
       method without yield then it will run in parallel. With Coroutine you
       have to be explicit when running parallel processes.

       Instead of:

           # run method in parallel
           defer = method()

           # wait for method result
           method_result = yield defer

       You have to write:

           # run method in parallel
           defer = webtwist.Coroutine(method()).start()

           # wait for method result
           method_result = yield defer

"""

import cgi, functools, logging, re, urllib, types

from twisted.internet import defer, reactor
from twisted.python import failure
from twisted.web import error, http, resource, server

log = logging.getLogger('webtwist')

def is_generator(value):
    return type(value) is types.GeneratorType

def to_generator(value):
    if is_generator(value):
        value = yield value

    raise StopIteration(value)

class Coroutine:

    _paused = 0
    _stopped = False

    _active_defer = None

    def __init__(self, gen):
        self._stack = [gen]
        self._defer = defer.Deferred()

    def _attach_deferred(self, deferred):
        # saving for producer actions
        self._active_defer = deferred

        waiting = [True, # waiting for result?
                   None] # result

        def peeker(result):
            if waiting[0]:
                waiting[0] = False
                waiting[1] = result

                return result

            # получили результат из Deferred
            del self._active_defer

            return self._resume(result)

        deferred.addBoth(peeker)

        if waiting[0]:
            # Haven't called back yet, set flag so that we get reinvoked
            waiting[0] = False
            return False, None

        # получили результат сразу
        del self._active_defer

        return True, waiting[1]

    def _resume(self, result):
        if self._stopped:
            # we have been stopped before start
            return

        while self._stack:
            gen = self._stack[-1]

            try:
                log.debug('Coroutine._resume in: %r', result)

                if isinstance(result, failure.Failure):
                    result = gen.throw(result.type, result.value, result.tb)
                else:
                    result = gen.send(result)

                log.debug('Coroutine._resume out: %r', result)

            except StopIteration, e:
                result = e.args[0] if len(e.args) == 1 else None
                del self._stack[-1]
                continue

            except:
                result = failure.Failure()
                del self._stack[-1]
                continue

            if is_generator(result):
                self._stack.append(result)
                result = None
                continue

            assert isinstance(result, defer.Deferred), 'Generators should yield generators or deferreds, got %r instead' % type(result)

            # пытаемся получить результат сразу, чтобы не уйти в глубину стека
            peeked, result = self._attach_deferred(result)
            if not peeked: return

        # конечный результат
        self._defer.callback(result)

    def start(self):
        if not self._paused:
            self._resume(None)

        return self._defer

    # интерфейс IPushProducer

    def pauseProducing(self):
        log.debug('Coroutine.pauseProducing')

        if self._active_defer is None:
            # not started yet, pause
            self._paused += 1
        else:
            # pause active deferred
            self._active_defer.pause()

    def resumeProducing(self):
        log.debug('Coroutine.resumeProducing')

        if self._active_defer is None:
            # not started yet, unpause
            self._paused -= 1

            if not self._paused:
                # and start coroutine
                self.start()
        else:
            # unpause active deferred
            self._active_defer.unpause()

    def stopProducing(self):
        log.debug('Coroutine.stopProducing')

        # setting flag to prevent resume with CancelledError or further start
        self._stopped = True

        if self._active_defer is not None:
            # cancel active deferred
            self._active_defer.cancel()

            # notify active generator about cancel
            self._stack[-1].close()

class Query:

    SEPARATOR_PATTERN = re.compile('[&;]')

    def __init__(self, query_string=None):
        # for correctness
        self._items = items = \
            () if query_string is None else tuple(self._iter_items(query_string))

        # for speed
        self._items_dict = dict(items)

    def _iter_items(self, query_string):
        unquote = lambda value: http.unquote(value.replace('+', ' '))

        for item in self.SEPARATOR_PATTERN.split(query_string):
            name_value = item.split('=', 1)

            if len(name_value) == 1:
                yield unquote(name_value[0]), None
            else:
                name, value = name_value
                yield unquote(name), unquote(value)

    def __repr__(self):
        if not self._items:
            return '%s()' % self.__class__.__name__

        return '%s(%r)' % (self.__class__.__name__, '&'.join(
            urllib.quote(key) if value is None else '%s=%s' % (urllib.quote(key), urllib.quote(value))
            for key, value in self._items
        ))

    def __getitem__(self, key):
        return self._items_dict[key]

    def get(self, key, default=None):
        return self._items_dict.get(key, default)

    def keys(self):
        return self._items_dict.keys()

    def __iter__(self):
        return iter(self._items_dict)

    def items(self):
        return self._items

    def values(self, key=None):
        if key is None:
            return self._items_dict.values()

        return tuple(value for item_key, value in self._items if item_key == key)

class Request(server.Request):

    query = Query()

    def requestReceived(self, command, path, version):
        # разбираем query
        if command == 'GET':
            path_query = path.split('?', 1)

            if len(path_query) == 2:
                self.query = Query(path_query[1])

        elif command == 'POST':
            ctype = self.requestHeaders.getRawHeaders('content-type')

            if ctype is not None and cgi.parse_header(ctype[0])[0] == 'application/x-www-form-urlencoded':
                self.content.seek(0)
                self.query = Query(self.content.read())

        server.Request.requestReceived(self, command, path, version)

class HandledResource(resource.Resource):

    def __init__(self, handler, handler_args):
        resource.Resource.__init__(self)

        self.handler = handler
        self.handler_args = handler_args

    def __repr__(self):
        return '%s(%r, %r)' % (self.__class__.__name__, self.handler, self.handler_args)

    def _render_coroutine(self, request, coroutine):
        def finish_request(result, failed=False):
            request.unregisterProducer()

            if failed:
                request.processingFailed(result)
            else:
                result = '' if result is None else str(result)

                if not request.startedWriting:
                    # чтобы не выводить как chunked
                    request.setHeader('content-length', str(len(result)))

                request.write(result)
                request.finish()

        request.registerProducer(coroutine, True)
        coroutine.start() \
            .addCallbacks(finish_request, finish_request, errbackKeywords={'failed': True})

        return server.NOT_DONE_YET

    def render(self, request):
        handler_args = {'request': request}
        handler_args.update(self.handler_args)

        result = self.handler(**handler_args)

        if is_generator(result):
            return self._render_coroutine(request, Coroutine(result))

        return ('' if result is None else str(result))

class UnsupportedMethodResource(resource.Resource):

    def __init__(self, allowed_methods):
        resource.Resource.__init__(self)

        self.allowed_methods = allowed_methods

    def __repr__(self):
        return u'%s(%r)' % (self.__class__.__name__, self.allowed_methods)

    def render(self, _request):
        raise error.UnsupportedMethod(self.allowed_methods)

PATTERN_PATTERN = re.compile(r'(?:([A-Z]+|\*)\s+)?(.+)')
PATH_PATTERN = re.compile(r'\{(?:(\{.+?\})|(.+?))\}')

def parse_route_pattern(pattern):
    """
        Allowed pattens:

            'METHOD /blog' -- serve METHOD request of /blog
            '/blog' -- shortcut for 'GET /blog'
            '* /blog' -- serve all commands applied to /blog
            'DELETE /blog/{date}' -- parse parameter from path

    """
    match = PATTERN_PATTERN.match(pattern)

    assert match, 'Invalid pattern format'

    method, path = match.groups()
    if method is None: method = 'GET'

    # паттерн для пути
    path_pattern = re.compile(''.join(
        '(?P<%s>.+?)' % re.escape(chunk) if (i % 3 == 2) else re.escape(chunk)
        for i, chunk in enumerate(PATH_PATTERN.split(path))
        if chunk is not None
    ) + '$')

    return method, path_pattern

def bind_site_handler(handler, site):
    """
        Bind handler to its site instance
    """
    @functools.wraps(handler)
    def wrapped(request, *args, **kwargs):
        request.site = site
        return handler(request=request, *args, **kwargs)

    return wrapped

class Site(server.Site):

    requestFactory = Request

    def __init__(self, *args, **kwargs):
        server.Site.__init__(self, resource=None, *args, **kwargs)
        self.routes = []

    def route(self, pattern, handler=None):
        method, path_pattern = parse_route_pattern(pattern)

        def add_route_handler(handler):
            self.routes.append((method, path_pattern, handler))
            return handler

        if handler is None:
            return add_route_handler

        add_route_handler(handler)

        return self

    def include(self, site, prefix=None):
        for method, path_pattern, handler in site.routes:
            if prefix is not None:
                path_pattern = re.compile(re.escape(prefix) + path_pattern.pattern)

            self.routes.append(
                (method, path_pattern, bind_site_handler(handler, site))
            )

    def _get_resource(self, method, path):
        allowed_methods = set()

        for route_method, path_pattern, handler in self.routes:
            match = path_pattern.match(path)
            if match is None: continue

            if route_method != '*' and route_method != method:
                allowed_methods.add(route_method)
                continue

            return HandledResource(handler, match.groupdict())

        if allowed_methods:
            return UnsupportedMethodResource(tuple(allowed_methods))

        return resource.NoResource()

    # overriding getResourceFor to retrieve resources via routes
    def getResourceFor(self, request):
        return self._get_resource(request.method, request.path)

# default site and its methods

site = Site()
route = site.route
include = site.include

def run(port, site=site):
    reactor.listenTCP(port, site)
    reactor.run()

def main():
    def test_handler(**kwargs):
        print kwargs

    for pattern in ('/foo/bar', '/{foo}/bar/{baz}', 'GET /foo', 'POST /foo/{z}/1', '/{{baz}}', '* /qq'):
        method, path_pattern = parse_route_pattern(pattern)
        print method, path_pattern.pattern

        site.route(pattern, test_handler)

    for request in ('GET /qq', 'QQ /foo/bar', 'QQ /1/bar/2', 'POST /foo/123/1'):
        print site._get_resource(*request.split(' ', 1))

if __name__ == '__main__': main()

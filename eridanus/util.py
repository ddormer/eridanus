import re, math, html5lib
from zope.interface import implements

try:
    from xml.etree import ElementTree
except ImportError:
    from elementtree import ElementTree

from twisted.internet import reactor, task, error as ineterror
from twisted.web import client, http, error as weberror
from twisted.python import log

from nevow.rend import Page, Fragment
from nevow.inevow import IResource, IRequest

from xmantissa.webtheme import _ThemedMixin, SiteTemplateResolver

from eridanus import const


class _PublicThemedMixin(_ThemedMixin):
    def getDocFactory(self, fragmentName, default=None):
        resolver = SiteTemplateResolver(self.store)
        return resolver.getDocFactory(fragmentName, default)


class ThemedPage(_PublicThemedMixin, Page):
    fragmentName = 'page-no-fragment-name-specified'

    def renderHTTP(self, ctx):
        if self.docFactory is None:
            self.docFactory = self.getDocFactory(self.fragmentName)
        return super(ThemedPage, self).renderHTTP(ctx)


class ThemedFragment(_PublicThemedMixin, Fragment):
    fragmentName = 'fragment-no-fragment-name-specified'

    def __init__(self, store, **kw):
        self.store = store
        super(ThemedFragment, self).__init__(**kw)


class PerseverantDownloader(object):
    maxDelay = 3600
    initialDelay = 1.0
    factor = 1.6180339887498948

    retryableHTTPCodes = [408, 500, 502, 503, 504]

    def __init__(self, url, tries=10, *a, **kw):
        self.url = sanitizeUrl(url)
        self.args = a
        self.kwargs = kw
        self.delay = self.initialDelay
        self.tries = tries

    def go(self):
        d, f = getPage(self.url, *self.args, **self.kwargs)
        return d.addErrback(self.retryWeb
               ).addCallback(lambda data: (data, f.response_headers))

    def retryWeb(self, f):
        f.trap((weberror.Error, ineterror.ConnectionDone))
        err = f.value
        if int(err.status) in self.retryableHTTPCodes:
            return self.retry(f)

        return f

    def retry(self, f):
        self.tries -= 1
        log.msg('PerseverantDownloader is retrying, %d attempts left.' % (self.tries,))
        log.err(f)
        self.delay = min(self.delay * self.factor, self.maxDelay)
        if self.tries == 0:
            return f

        return task.deferLater(reactor, self.delay, self.go)


def encode(s):
    return s.encode(const.ENCODING, 'replace')


def decode(s):
    return s.decode(const.ENCODING, 'replace')


def handle206(f):
    f.trap(weberror.Error)
    err = f.value
    try:
        if int(err.status) == http.PARTIAL_CONTENT:
            return err.response
    except ValueError:
        pass

    return f


def sanitizeUrl(url):
    if '#' in url:
        url = url[:url.index('#')]
    return url


def getPage(url, contextFactory=None, *args, **kwargs):
    scheme, host, port, path = client._parse(url)
    factory = client.HTTPClientFactory(url, *args, **kwargs)
    if scheme == 'https':
        from twisted.internet import ssl
        if contextFactory is None:
            contextFactory = ssl.ClientContextFactory()
        reactor.connectSSL(host, port, factory, contextFactory)
    else:
        reactor.connectTCP(host, port, factory)
    return factory.deferred.addErrback(handle206), factory


_whitespace = re.compile(ur'\s+')

def sanitizeTitle(title):
    return _whitespace.sub(u' ', title.strip())


def extractTitle(data):
    if data:
        try:
            parser = html5lib.HTMLParser(tree=html5lib.treebuilders.getTreeBuilder('etree', ElementTree))
            tree = ElementTree.ElementTree(parser.parse(data))
            titleElem = tree.find('//title')
            if titleElem is not None and titleElem.text is not None:
                text = unicode(titleElem.text)
                return sanitizeTitle(text)
        except:
            log.msg('Extracting title failed:')
            log.err()

    return None


def truncate(s, limit):
    if len(s) - 3 < limit:
        return s

    return s[:limit] + '...'


def prettyTimeDelta(d):
    days = d.days

    seconds = d.seconds

    hours = seconds // 3600
    seconds -= hours * 3600

    minutes = seconds // 60
    seconds -= minutes * 60

    s = []
    if days:
        s.append('%d days' % (days,))
    if hours:
        s.append('%d hours' % (hours,))
    if minutes:
        s.append('%d minutes' % (minutes,))
    if seconds:
        s.append('%d seconds' % (seconds,))

    if not s:
        s.append('never')

    return ' '.join(s)


# XXX: this needs to be hooked up at a global level, otherwise a new resource
#      gets created for each page, defeating the point of caching.
class CachingResource(object):
    implements(IResource)

    def __init__(self, contentGenerator, timeToLive):
        self.contentGenerator = contentGenerator
        self.timeToLive = timeToLive
        self.updateCall = None
        self.updateContent()

    def updateContent(self):
        if self.updateCall is not None and self.updateCall.active():
            self.updateCall.cancel()

        self.resourceInfo = self.contentGenerator()
        self.updateCall = reactor.callLater(self.timeToLive, self.updateContent)

    ### IResource

    def locateChild(self, ctx, segments):
        return None

    def renderHTTP(self, ctx):
        req = IRequest(ctx)

        hasContentLength = False
        data, headers = self.resourceInfo
        for key, value in headers.iteritems():
            if not hasContentLength and key.lower() == 'content-length':
                hasContentLength = True
            req.setHeader(key, value)

        if not hasContentLength:
            req.setHeader('Content-Length', len(data))

        return data


sizePrefixes = (u'bytes', u'KB', u'MB', u'GB', u'TB', u'PB', u'EB', u'ZB', u'YB')

def humanReadableFileSize(size):
    factor = int(math.log(size, 1024))
    return u'%0.2f%s' % (size / (1024.0 ** factor), sizePrefixes[factor])

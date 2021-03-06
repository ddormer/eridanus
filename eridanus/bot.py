import itertools

from zope.interface import implements

from twisted.application.service import IService, IServiceCollection
from twisted.cred.checkers import AllowAnonymousAccess
from twisted.cred.credentials import UsernamePassword
from twisted.cred.portal import Portal
from twisted.internet import reactor, error as ierror
from twisted.internet.defer import succeed, maybeDeferred, Deferred
from twisted.internet.protocol import ReconnectingClientFactory
from twisted.python import log
from twisted.words.protocols.irc import IRCClient

from axiom import errors as aerrors
from axiom.attributes import (integer, inmemory, reference, bytes, text,
    textlist)
from axiom.dependency import dependsOn
from axiom.item import Item
from axiom.upgrade import registerUpgrader, registerAttributeCopyingUpgrader
from axiom.userbase import LoginSystem

from eridanus import util, errors, plugin, iriparse
from eridanus.irc import IRCSource, IRCUser
from eridanus.ieridanus import ICommand, IIRCAvatar
from eridanus.plugin import usage, rest, SubCommand, IncrementalArguments
from eridanus.util import encode, decode



class IRCBot(IRCClient):
    isupportStrings = [
        'are available on this server',
        'are supported by this server']

    def __init__(self, appStore, serviceID, factory, portal, config):
        self.serviceID = serviceID
        self.factory = factory
        self.portal = portal
        self.config = config
        self.appStore = appStore
        self.nickname = encode(config.nickname)

        self.topicDeferreds = {}
        self.isupported = {}
        self.authenticatedUsers = {}


    def maxMessageLength(self):
        # XXX: This should probably take into account the prefix we are about
        # to use or something.
        return 500 - int(self.isupported['NICKLEN'][0]) - int(self.isupported['CHANNELLEN'][0])


    def irc_RPL_BOUNCE(self, prefix, params):
        # 005 is doubly assigned.  Piece of crap dirty trash protocol.
        if params[-1] in self.isupportStrings:
            self.isupport(params[1:-1])
        else:
            self.bounce(params[1])


    def join(self, channel, key=None):
        self.config.addChannel(channel)
        return IRCClient.join(self, encode(channel), key)


    def part(self, channel):
        self.config.removeChannel(channel)
        return IRCClient.part(self, encode(channel))


    def ignore(self, mask):
        return self.config.addIgnore(mask)


    def unignore(self, mask):
        return self.config.removeIgnore(mask)


    def noticed(self, user, channel, message):
        pass


    def broadcastAmbientEvent(self, eventName, source, *args, **kw):
        """
        Broadcast an ambient event to all L{IAmbientEventObserver}s.

        @type  eventName: C{str}
        @param eventName: Event to broadcast, this is assumed to be a callable
            attribute on the L{IAmbientEventObserver}.

        @type source: L{IRCSource}

        @param *args: Additional arguments to pass to the event observer.

        @param *kw: Additional keyword arguments to pass to the event observer.

        @rtype: C{Deferred}
        """
        plugin.broadcastAmbientEvent(
            self.appStore, eventName, source, *args, **kw)


    def joined(self, channel):
        source = IRCSource(self, decode(channel), None)
        self.broadcastAmbientEvent('joinedChannel', source)


    def privmsg(self, user, channel, message):
        user = IRCUser(user)
        if self.config.isIgnored(user.usermask):
            return

        source = IRCSource(self, decode(channel), user)
        message = decode(message)

        directedTextSuffixes = (':', ',')
        isDirected = False
        for suffix in directedTextSuffixes:
            directedText = decode(self.nickname.lower()) + suffix
            if message.lower().startswith(directedText):
                isDirected = True
                break

        if isDirected:
            # Remove our nickname from the beginning of the addressed text.
            message = message[len(directedText):].strip()

        if source.isPrivate:
            self.privateMessage(source, message)
        else:
            if isDirected:
                self.directedPublicMessage(source, message)
            else:
                self.publicMessage(source, message)


    def topic(self, channel, topic=None):
        channel = encode(channel)
        if topic is not None:
            topic = encode(topic)

        d = self.topicDeferreds.get(channel)
        if d is None:
            d = self.topicDeferreds[channel] = Deferred()
        IRCClient.topic(self, channel, topic)
        return d


    def topicUpdated(self, user, channel, topic):
        d = self.topicDeferreds.pop(channel, None)
        if d is not None:
            if topic is not None:
                topic = decode(topic)
            d.callback((user, channel, topic))


    def isupport(self, options):
        isupported = self.isupported
        for param in options:
            if '=' in param:
                key, value = param.split('=', 1)
                value = value.split(',')
            else:
                key = param
                value = True
            isupported[key] = value


    def setModes(self):
        for mode in self.config.modes:
            self.mode(self.nickname, True, mode)


    def signedOn(self):
        log.msg('Signed on.')
        self.factory.resetDelay()

        self.setModes()

        channels = self.config.channels
        for channel in channels:
            self.join(encode(channel))

        log.msg('Joined channels: %r' % (channels,))


    def locatePlugin(self, name):
        """
        Get a C{IEridanusPlugin} provider by name.
        """
        return plugin.getPluginByName(self.appStore, name)


    def command(self, source, message):
        """
        Find and invoke the C{ICommand} provider from C{message}.
        """
        return plugin.command(self.appStore, source, message)


    def mentionFailure(self, f, source, msg=None):
        if msg is not None:
            log.msg(msg)
        log.err(f)
        msg = '%s: %s' % (f.type.__name__, f.getErrorMessage())
        source.say(msg)


    def directedPublicMessage(self, source, message):
        maybeDeferred(self.command, source, message
            ).addErrback(source.logFailure)

    privateMessage = directedPublicMessage


    def publicMessage(self, source, message):
        self.broadcastAmbientEvent('publicMessageReceived', source, message)
        for url in iriparse.parseURLs(message):
            self.broadcastAmbientEvent('publicURLReceived', source, url)


    def getAuthenticatedAvatar(self, nickname):
        avatar, logout = self._getAvatar(nickname)
        if avatar is None:
            raise errors.AuthenticationError(u'"%s" is not authenticated or has no avatar' % (nickname,))
        return avatar


    def logout(self, nickname):
        avatar, logout = self._getAvatar(nickname)
        if logout is not None:
            logout()
            return True

        return False


    def login(self, nickname, password):
        username = self.getUsername(nickname)

        def failedLogin(f):
            f.trap(aerrors.UnauthorizedLogin)
            log.msg('Authentication for "%s" failed:' % (username,))
            log.err(f)
            raise errors.AuthenticationError(u'Unable to authenticate "%s"' % (username,))

        def wrapLogout(self, logout):
            def _logout():
                del self.authenticatedUsers[username]
                logout()
            return _logout

        def loginDone((interface, avatar, logout)):
            self.logout(username)
            logout = wrapLogout(self, logout)
            self.authenticatedUsers[username] = (avatar, logout)

        d = self.portal.login(
            UsernamePassword(username, password),
            None,
            IIRCAvatar)

        return d.addCallbacks(loginDone, failedLogin)


    def grantPlugin(self, nickname, pluginName):
        """
        Grant access to a plugin.

        @type nickname: C{unicode} or C{None}
        @param nickname: Nickname to grant access to, or C{None} if global
            access should be granted

        @type pluginName: C{unicode}
        @param pluginName: Plugin name to grant access to
        """
        if nickname is None:
            store = self.appStore
        else:
            store = self.getAuthenticatedAvatar(nickname).store
        plugin.installPlugin(store, pluginName)


    def diagnosePlugin(self, pluginName):
        """
        Diagnose a broken plugin.

        @type pluginName: C{unicode}
        @param pluginName: Plugin name to diagnose

        @returns: C{twisted.python.failure.Failure} instance from broken
            plugin.
        """
        return plugin.diagnoseBrokenPlugin(pluginName)


    def revokePlugin(self, nickname, pluginName):
        """
        Revoke access to a plugin.

        @type nickname: C{unicode} or C{None}
        @param nickname: Nickname to revoke access from, or C{None} if global
            access should be revoked

        @type pluginName: C{unicode}
        @param pluginName: Plugin name to revoke access to
        """
        if nickname is None:
            store = self.appStore
        else:
            store = self.getAuthenticatedAvatar(nickname).store
        plugin.uninstallPlugin(store, pluginName)


    def getAvailablePlugins(self, nickname):
        """
        Get an iterable of names of plugins that can still be installed.
        """
        def pluginTypes(it):
            return (type(p) for p in it)

        installedPlugins = set(pluginTypes(plugin.getInstalledPlugins(self.appStore)))
        avatar = self.getAvatar(nickname)
        # XXX: This is a crap way to tell the difference between authenticated
        # users and plebs.  Fix it!
        if hasattr(avatar, 'store'):
            installedPlugins.update(pluginTypes(plugin.getInstalledPlugins(avatar.store)))

        allPlugins = set(plugin.getAllPlugins())
        return (p.pluginName for p in allPlugins - installedPlugins)


    def getBrokenPlugins(self):
        """
        Get an iterable of names of plugins that cannot be installed.
        """
        brokenPlugins = set(plugin.getBrokenPlugins())
        return (p.pluginName for p in brokenPlugins)



class IRCBotFactory(ReconnectingClientFactory):
    protocol = IRCBot

    noisy = True

    def __init__(self, service, portal, config):
        self.service = service

        # XXX: should this be here?
        appStore = service.loginSystem.accountByAddress(u'Eridanus', None).avatars.open()
        self.bot = self.protocol(appStore, service.serviceID, self, portal, config)


    @property
    def connector(self):
        return self.service.connector


    def buildProtocol(self, addr=None):
        return self.bot



class IRCBotFactoryFactory(Item):
    schemaVersion = 1

    dummy = integer()

    def getFactory(self, service, portal, config):
        return IRCBotFactory(service, portal, config)



class IRCBotConfig(Item):
    typeName = 'eridanus_ircbotconfig'
    schemaVersion = 5

    name = text(doc="""
    The name of the network this config is for.
    """)

    hostname = bytes(doc="""
    The hostname of the IRC server to connect to.
    """)

    portNumber = integer(doc="""
    The port to connect to the IRC server on.
    """)

    nickname = text(doc="""
    The bot's nickname.
    """)

    channels = textlist(doc="""
    A C{list} of channels the bot should join.
    """, default=[])

    ignores = textlist(doc="""
    A C{list} of masks to ignore.
    """, default=[])

    modes = bytes(doc="""
    A string of user modes to set after successfully connecting to C{hostname}.
    """, default='B')

    def addChannel(self, channel):
        if channel not in self.channels:
            self.channels = self.channels + [channel]


    def removeChannel(self, channel):
        channels = self.channels
        while channel in channels:
            channels.remove(channel)
        self.channels = channels


    def isIgnored(self, mask):
        mask = util.normalizeMask(mask)
        for ignore in self.ignores:
            ignore = util.normalizeMask(ignore)
            if util.hostMatches(mask, ignore):
                return True

        return False


    def addIgnore(self, mask):
        mask = util.normalizeMask(mask)
        if mask not in self.ignores:
            self.ignores = self.ignores + [mask]
            return mask
        return None


    def removeIgnore(self, mask):
        def removeIgnores(mask):
            for ignore in self.ignores:
                normalizedIgnore = util.normalizeMask(ignore)
                if not util.hostMatches(normalizedIgnore, mask):
                    yield ignore

        mask = util.normalizeMask(mask)
        newIgnores = list(removeIgnores(mask))
        diff = set(self.ignores) - set(newIgnores)
        self.ignores = newIgnores
        return list(diff) or None


def ircbotconfig1to2(old):
    return old.upgradeVersion(
        IRCBotConfig.typeName, 1, 2,
        hostname=old.hostname,
        portNumber=old.portNumber,
        nickname=old.nickname.decode('utf-8'),
        _channels=old._channels.decode('utf-8'),
        _ignores=old._ignores.decode('utf-8'))

registerUpgrader(ircbotconfig1to2, IRCBotConfig.typeName, 1, 2)



def ircbotconfig2to3(old):
    return old.upgradeVersion(
        IRCBotConfig.typeName, 2, 3,
        hostname=old.hostname,
        portNumber=old.portNumber,
        nickname=old.nickname,
        channels=old._channels.split(u','),
        ignores=old._ignores.split(u','))

registerUpgrader(ircbotconfig2to3, IRCBotConfig.typeName, 2, 3)
registerAttributeCopyingUpgrader(IRCBotConfig, 3, 4)
registerAttributeCopyingUpgrader(IRCBotConfig, 4, 5)



class IRCBotService(Item):
    implements(IService)

    typeName = 'eridanus_ircbotservice'
    schemaVersion = 1

    powerupInterfaces = [IService]

    name = None

    serviceID = bytes(doc="""
    """, allowNone=False)

    config = reference(doc="""
    """)

    parent = inmemory(doc="""
    The parent of this service.
    """)

    factory = reference(doc="""
    An L{Item} with a C{getFactory} method which returns a Twisted protocol
    factory.
    """, whenDeleted=reference.CASCADE)

    connector = inmemory(doc="""
    The L{IConnector} returned by C{reactor.connectTCP}.
    """)

    portal = inmemory(doc="""
    """)

    loginSystem = dependsOn(LoginSystem)

    def connect(self):
        config = self.config
        assert config is not None, 'No configuration data'

        hostname = config.hostname
        port = config.portNumber

        log.msg('Connecting to %s (%s:%s) as %r' % (config.name, hostname, port, config.nickname))
        return reactor.connectTCP(hostname, port, self.factory.getFactory(self, self.portal, config))


    def disconnect(self):
        self.connector.disconnect()


    def activate(self):
        self.parent = None
        self.connector = None
        if self.loginSystem:
            self.portal = Portal(self.loginSystem, [self.loginSystem, AllowAnonymousAccess()])


    def installed(self):
        self.setServiceParent(self.store)


    def deleted(self):
        if self.parent is not None:
            self.disownServiceParent()


    ### IService

    def setServiceParent(self, parent):
        IServiceCollection(parent).addService(self)
        self.parent = parent


    def disownServiceParent(self):
        IServiceCollection(self.parent).removeService(self)
        self.parent = None


    def privilegedStartService(self):
        pass


    def startService(self):
        if self.connector is None:
            self.connector = self.connect()


    def stopService(self):
        self.disconnect()
        return succeed(None)

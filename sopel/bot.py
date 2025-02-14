# coding=utf-8
# Copyright 2008, Sean B. Palmer, inamidst.com
# Copyright © 2012, Elad Alfassa <elad@fedoraproject.org>
# Copyright 2012-2015, Elsie Powell, http://embolalia.com
# Copyright 2019, Florian Strzelecki <florian.strzelecki@gmail.com>
#
# Licensed under the Eiffel Forum License 2.

from __future__ import unicode_literals, absolute_import, print_function, division

from ast import literal_eval
import collections
from datetime import datetime
import itertools
import logging
import re
import sys
import threading
import time

from sopel import irc, logger, plugins, tools
from sopel.db import SopelDB
from sopel.tools import Identifier, deprecated
import sopel.tools.jobs
from sopel.trigger import Trigger
from sopel.module import NOLIMIT
import sopel.loader


__all__ = ['Sopel', 'SopelWrapper']

LOGGER = logging.getLogger(__name__)

if sys.version_info.major >= 3:
    unicode = str
    basestring = str
    py3 = True
else:
    py3 = False


class Sopel(irc.AbstractBot):
    def __init__(self, config, daemon=False):
        super(Sopel, self).__init__(config)
        self._daemon = daemon  # Used for iPython. TODO something saner here
        self.wantsrestart = False

        # `re.compile('.*') is re.compile('.*')` because of caching, so we need
        # to associate a list with each regex, since they are unexpectedly
        # indistinct.
        self._callables = {
            'high': collections.defaultdict(list),
            'medium': collections.defaultdict(list),
            'low': collections.defaultdict(list)
        }
        self._plugins = {}

        self.doc = {}
        """A dictionary of command names to their documentation.

        Each command is mapped to its docstring and any available examples, if
        declared in the module's code.

        .. versionchanged:: 3.2
            Use the first item in each callable's commands list as the key,
            instead of the function name as declared in the source code.
        """

        self._command_groups = collections.defaultdict(list)
        """A mapping of module names to a list of commands in it."""

        self.stats = {}  # deprecated, remove in 7.0
        self._times = {}
        """
        A dictionary mapping lowercased nicks to dictionaries which map
        function names to the time which they were last used by that nick.
        """

        self.server_capabilities = {}
        """A dict mapping supported IRCv3 capabilities to their options.

        For example, if the server specifies the capability ``sasl=EXTERNAL``,
        it will be here as ``{"sasl": "EXTERNAL"}``. Capabilities specified
        without any options will have ``None`` as the value.

        For servers that do not support IRCv3, this will be an empty set.
        """

        self.privileges = dict()
        """A dictionary of channels to their users and privilege levels.

        The value associated with each channel is a dictionary of
        :class:`sopel.tools.Identifier`\\s to
        a bitwise integer value, determined by combining the appropriate
        constants from :mod:`sopel.module`.

        .. deprecated:: 6.2.0
            Use :attr:`channels` instead. Will be removed in Sopel 8.
        """

        self.channels = tools.SopelMemory()  # name to chan obj
        """A map of the channels that Sopel is in.

        The keys are :class:`sopel.tools.Identifier`\\s of the channel names,
        and map to :class:`sopel.tools.target.Channel` objects which contain
        the users in the channel and their permissions.
        """

        self.users = tools.SopelMemory()  # name to user obj
        """A map of the users that Sopel is aware of.

        The keys are :class:`sopel.tools.Identifier`\\s of the nicknames, and
        map to :class:`sopel.tools.target.User` instances. In order for Sopel
        to be aware of a user, it must be in at least one channel which they
        are also in.
        """

        self.db = SopelDB(config)
        """The bot's database, as a :class:`sopel.db.SopelDB` instance."""

        self.memory = tools.SopelMemory()
        """
        A thread-safe dict for storage of runtime data to be shared between
        modules. See :class:`sopel.tools.SopelMemory`.
        """

        self.shutdown_methods = []
        """List of methods to call on shutdown."""

        self.scheduler = sopel.tools.jobs.JobScheduler(self)
        """Job Scheduler. See :func:`sopel.module.interval`."""

        # Set up block lists
        # Default to empty
        if not self.settings.core.nick_blocks:
            self.settings.core.nick_blocks = []
        if not self.settings.core.host_blocks:
            self.settings.core.host_blocks = []

    @property
    def command_groups(self):
        """A mapping of module names to a list of commands in it."""
        # This was supposed to be deprecated, but the help command uses this
        return self._command_groups

    @property
    def hostmask(self):
        """The current hostmask for the bot :class:`sopel.tools.target.User`.

        :return: the bot's current hostmask
        :rtype: str

        Bot must be connected and in at least one channel.
        """
        if not self.users or self.nick not in self.users:
            raise KeyError("'hostmask' not available: bot must be connected and in at least one channel.")

        return self.users.get(self.nick).hostmask

    def setup(self):
        """Set up Sopel bot before it can run

        The setup phase manages to:

        * setup logging (configure Python's built-in :mod:`logging`),
        * setup the bot's plugins (load, setup, and register)
        * start the job scheduler

        """
        self.setup_logging()
        self.setup_plugins()
        self.scheduler.start()

    def setup_logging(self):
        logger.setup_logging(self.settings)
        base_level = self.settings.core.logging_level or 'INFO'
        base_format = self.settings.core.logging_format
        base_datefmt = self.settings.core.logging_datefmt

        # configure channel logging if required by configuration
        if self.settings.core.logging_channel:
            channel_level = self.settings.core.logging_channel_level or base_level
            channel_format = self.settings.core.logging_channel_format or base_format
            channel_datefmt = self.settings.core.logging_channel_datefmt or base_datefmt
            channel_params = {}
            if channel_format:
                channel_params['fmt'] = channel_format
            if channel_datefmt:
                channel_params['datefmt'] = channel_datefmt
            formatter = logger.ChannelOutputFormatter(**channel_params)
            handler = logger.IrcLoggingHandler(self, channel_level)
            handler.setFormatter(formatter)

            # set channel handler to `sopel` logger
            LOGGER = logging.getLogger('sopel')
            LOGGER.addHandler(handler)

    def setup_plugins(self):
        load_success = 0
        load_error = 0
        load_disabled = 0

        LOGGER.info('Loading plugins...')
        usable_plugins = plugins.get_usable_plugins(self.settings)
        for name, info in usable_plugins.items():
            plugin, is_enabled = info
            if not is_enabled:
                load_disabled = load_disabled + 1
                continue

            try:
                plugin.load()
            except Exception as e:
                load_error = load_error + 1
                LOGGER.exception('Error loading %s: %s', name, e)
            else:
                try:
                    if plugin.has_setup():
                        plugin.setup(self)
                    plugin.register(self)
                except Exception as e:
                    load_error = load_error + 1
                    LOGGER.exception('Error in %s setup: %s', name, e)
                else:
                    load_success = load_success + 1
                    LOGGER.info('Plugin loaded: %s', name)

        total = sum([load_success, load_error, load_disabled])
        if total and load_success:
            LOGGER.info(
                'Registered %d plugins, %d failed, %d disabled',
                (load_success - 1),
                load_error,
                load_disabled)
        else:
            LOGGER.warning("Warning: Couldn't load any plugins")

    def reload_plugin(self, name):
        """Reload a plugin

        :param str name: name of the plugin to reload
        :raise PluginNotRegistered: when there is no ``name`` plugin registered

        It runs the plugin's shutdown routine and unregisters it. Then it
        reloads it, runs its setup routines, and registers it again.
        """
        if not self.has_plugin(name):
            raise plugins.exceptions.PluginNotRegistered(name)

        plugin = self._plugins[name]
        # tear down
        plugin.shutdown(self)
        plugin.unregister(self)
        LOGGER.info('Unloaded plugin %s', name)
        # reload & setup
        plugin.reload()
        plugin.setup(self)
        plugin.register(self)
        LOGGER.info('Reloaded plugin %s', name)

    def reload_plugins(self):
        """Reload all plugins

        First, run all plugin shutdown routines and unregister all plugins.
        Then reload all plugins, run their setup routines, and register them
        again.
        """
        registered = list(self._plugins.items())
        # tear down all plugins
        for name, plugin in registered:
            plugin.shutdown(self)
            plugin.unregister(self)
            LOGGER.info('Unloaded plugin %s', name)

        # reload & setup all plugins
        for name, plugin in registered:
            plugin.reload()
            plugin.setup(self)
            plugin.register(self)
            LOGGER.info('Reloaded plugin %s', name)

    def add_plugin(self, plugin, callables, jobs, shutdowns, urls):
        """Add a loaded plugin to the bot's registry"""
        self._plugins[plugin.name] = plugin
        self.register(callables, jobs, shutdowns, urls)

    def remove_plugin(self, plugin, callables, jobs, shutdowns, urls):
        """Remove a loaded plugin from the bot's registry"""
        name = plugin.name
        if not self.has_plugin(name):
            raise plugins.exceptions.PluginNotRegistered(name)

        # remove commands, jobs, and shutdown functions
        for func in itertools.chain(callables, jobs, shutdowns):
            self.unregister(func)

        # remove URL callback handlers
        if "url_callbacks" in self.memory:
            for func in urls:
                regexes = func.url_regex
                for regex in regexes:
                    if func == self.memory['url_callbacks'].get(regex):
                        self.unregister_url_callback(regex)
                        LOGGER.debug('URL Callback unregistered: %r', regex)

        # remove plugin from registry
        del self._plugins[name]

    def has_plugin(self, name):
        """Tell if the bot has registered this plugin by its name"""
        return name in self._plugins

    def unregister(self, obj):
        """Unregister a callable.

        :param obj: the callable to unregister
        :type obj: :term:`object`
        """
        if not callable(obj):
            LOGGER.warning('Cannot unregister obj %r: not a callable', obj)
            return
        callable_name = getattr(obj, "__name__", 'UNKNOWN')

        if hasattr(obj, 'rule'):  # commands and intents have it added
            for rule in obj.rule:
                callb_list = self._callables[obj.priority][rule]
                if obj in callb_list:
                    callb_list.remove(obj)
            LOGGER.debug(
                'Rule callable "%s" unregistered',
                callable_name,
                rule.pattern)

        if hasattr(obj, 'interval'):
            self.scheduler.remove_callable_job(obj)
            LOGGER.debug('Job callable removed: %s', callable_name)

        if callable_name == "shutdown" and obj in self.shutdown_methods:
            self.shutdown_methods.remove(obj)

    def register(self, callables, jobs, shutdowns, urls):
        """Register rules, jobs, shutdown methods, and URL callbacks.

        :param callables: an iterable of callables to register
        :type callables: :term:`iterable`
        :param jobs: an iterable of functions to periodically invoke
        :type jobs: :term:`iterable`
        :param shutdowns: an iterable of functions to call on shutdown
        :type shutdowns: :term:`iterable`
        :param urls: an iterable of functions to call when matched against a URL
        :type urls: :term:`iterable`

        The ``callables`` argument contains a list of "callable objects", i.e.
        objects for which :func:`callable` will return ``True``. They can be:

        * a callable with rules (will match triggers with a regex pattern)
        * a callable without rules (will match any triggers, such as events)
        * a callable with commands
        * a callable with nick commands
        * a callable with action commands

        It is possible to have a callable with rules, commands, and nick
        commands configured. It should not be possible to have a callable with
        commands or nick commands but without rules. Callables without rules
        are usually event handlers.
        """
        # Append module's shutdown function to the bot's list of functions to
        # call on shutdown
        self.shutdown_methods += shutdowns
        match_any = re.compile('.*')
        for callbl in callables:
            callable_name = getattr(callbl, "__name__", 'UNKNOWN')
            rules = getattr(callbl, 'rule', [])
            commands = getattr(callbl, 'commands', [])
            nick_commands = getattr(callbl, 'nickname_commands', [])
            action_commands = getattr(callbl, 'action_commands', [])
            events = getattr(callbl, 'event', [])
            is_rule_only = rules and not commands and not nick_commands

            if rules:
                for rule in rules:
                    self._callables[callbl.priority][rule].append(callbl)
                    if is_rule_only:
                        # Command & Nick Command are logged later:
                        # here we log rule only callable
                        LOGGER.debug(
                            'Rule callable "%s" registered for "%s"',
                            callable_name,
                            rule.pattern)
                if commands:
                    LOGGER.debug(
                        'Command callable "%s" registered for "%s"',
                        callable_name,
                        '|'.join(commands))
                if nick_commands:
                    LOGGER.debug(
                        'Nick command callable "%s" registered for "%s"',
                        callable_name,
                        '|'.join(nick_commands))
                if action_commands:
                    LOGGER.debug(
                        'Action command callable "%s" registered for "%s"',
                        callable_name,
                        '|'.join(action_commands))
                if events:
                    LOGGER.debug(
                        'Event callable "%s" registered for "%s"',
                        callable_name,
                        '|'.join(events))
            else:
                self._callables[callbl.priority][match_any].append(callbl)
                if events:
                    LOGGER.debug(
                        'Event callable "%s" registered '
                        'with "match any" rule for "%s"',
                        callable_name,
                        '|'.join(events))
                else:
                    LOGGER.debug(
                        'Rule callable "%s" registered with "match any" rule',
                        callable_name)

            if commands:
                module_name = callbl.__module__.rsplit('.', 1)[-1]
                # TODO doc and make decorator for this. Not sure if this is how
                # it should work yet, so not making it public for 6.0.
                category = getattr(callbl, 'category', module_name)
                self._command_groups[category].append(commands[0])

            for command, docs in callbl._docs.items():
                self.doc[command] = docs

        for func in jobs:
            for interval in func.interval:
                job = sopel.tools.jobs.Job(interval, func)
                self.scheduler.add_job(job)
                callable_name = getattr(func, "__name__", 'UNKNOWN')
                LOGGER.debug(
                    'Job added "%s", will run every %d seconds',
                    callable_name,
                    interval)

        for func in urls:
            for regex in func.url_regex:
                self.register_url_callback(regex, func)
                callable_name = getattr(func, "__name__", 'UNKNOWN')
                LOGGER.debug(
                    'URL Callback added "%s" for URL pattern "%s"',
                    callable_name,
                    regex)

    @deprecated
    def msg(self, recipient, text, max_messages=1):
        """
        .. deprecated:: 6.0
            Use :meth:`say` instead. Will be removed in Sopel 8.
        """
        self.say(text, recipient, max_messages)

    def call(self, func, sopel, trigger):
        """Call a function, applying any rate-limiting or restrictions.

        :param func: the function to call
        :type func: :term:`function`
        :param sopel: a SopelWrapper instance
        :type sopel: :class:`SopelWrapper`
        :param Trigger trigger: the Trigger object for the line from the server
                                that triggered this call
        """
        nick = trigger.nick
        current_time = time.time()
        if nick not in self._times:
            self._times[nick] = dict()
        if self.nick not in self._times:
            self._times[self.nick] = dict()
        if not trigger.is_privmsg and trigger.sender not in self._times:
            self._times[trigger.sender] = dict()

        if not trigger.admin and not func.unblockable:
            if func in self._times[nick]:
                usertimediff = current_time - self._times[nick][func]
                if func.rate > 0 and usertimediff < func.rate:
                    LOGGER.info(
                        "%s prevented from using %s in %s due to user limit: %d < %d",
                        trigger.nick, func.__name__, trigger.sender, usertimediff,
                        func.rate
                    )
                    return
            if func in self._times[self.nick]:
                globaltimediff = current_time - self._times[self.nick][func]
                if func.global_rate > 0 and globaltimediff < func.global_rate:
                    LOGGER.info(
                        "%s prevented from using %s in %s due to global limit: %d < %d",
                        trigger.nick, func.__name__, trigger.sender, globaltimediff,
                        func.global_rate
                    )
                    return

            if not trigger.is_privmsg and func in self._times[trigger.sender]:
                chantimediff = current_time - self._times[trigger.sender][func]
                if func.channel_rate > 0 and chantimediff < func.channel_rate:
                    LOGGER.info(
                        "%s prevented from using %s in %s due to channel limit: %d < %d",
                        trigger.nick, func.__name__, trigger.sender, chantimediff,
                        func.channel_rate
                    )
                    return

        # if channel has its own config section, check for excluded modules/modules methods
        if trigger.sender in self.config:
            channel_config = self.config[trigger.sender]

            # disable listed modules completely on provided channel
            if 'disable_modules' in channel_config:
                disabled_modules = channel_config.disable_modules.split(',')

                # if "*" is used, we are disabling all modules on provided channel
                if '*' in disabled_modules:
                    return
                if func.__module__ in disabled_modules:
                    return

            # disable chosen methods from modules
            if 'disable_commands' in channel_config:
                disabled_commands = literal_eval(channel_config.disable_commands)

                if func.__module__ in disabled_commands:
                    if func.__name__ in disabled_commands[func.__module__]:
                        return

        try:
            exit_code = func(sopel, trigger)
        except Exception as error:  # TODO: Be specific
            exit_code = None
            self.error(trigger, exception=error)

        if exit_code != NOLIMIT:
            self._times[nick][func] = current_time
            self._times[self.nick][func] = current_time
            if not trigger.is_privmsg:
                self._times[trigger.sender][func] = current_time

    def dispatch(self, pretrigger):
        """Dispatch a parsed message to any registered callables.

        :param PreTrigger pretrigger: a parsed message from the server
        """
        args = pretrigger.args
        text = args[-1] if args else ''
        event = pretrigger.event
        intent = pretrigger.tags.get('intent')
        nick = pretrigger.nick
        is_echo_message = nick.lower() == self.nick.lower()
        user_obj = self.users.get(nick)
        account = user_obj.account if user_obj else None

        if self.config.core.nick_blocks or self.config.core.host_blocks:
            nick_blocked = self._nick_blocked(pretrigger.nick)
            host_blocked = self._host_blocked(pretrigger.host)
        else:
            nick_blocked = host_blocked = None
        blocked = bool(nick_blocked or host_blocked)

        list_of_blocked_functions = []
        for priority in ('high', 'medium', 'low'):
            for regexp, funcs in self._callables[priority].items():
                match = regexp.match(text)
                if not match:
                    continue

                for func in funcs:
                    trigger = Trigger(self.config, pretrigger, match, account)

                    # check event
                    if event not in func.event:
                        continue

                    # check intents
                    if hasattr(func, 'intents'):
                        if not intent:
                            continue

                        match = any(
                            func_intent.match(intent)
                            for func_intent in func.intents
                        )
                        if not match:
                            continue

                    # check echo-message feature
                    if is_echo_message and not func.echo:
                        continue

                    # check blocked nick/host
                    # done after we know the trigger would have matched so we
                    # don't spam logs with "prevented from using" entries about
                    # functions that weren't going to run anyway
                    if blocked and not func.unblockable and not trigger.admin:
                        function_name = "%s.%s" % (
                            func.__module__, func.__name__
                        )
                        list_of_blocked_functions.append(function_name)
                        continue

                    # call triggered function
                    wrapper = SopelWrapper(self, trigger)
                    if func.thread:
                        targs = (func, wrapper, trigger)
                        t = threading.Thread(target=self.call, args=targs)
                        t.start()
                    else:
                        self.call(func, wrapper, trigger)

        if list_of_blocked_functions:
            if nick_blocked and host_blocked:
                block_type = 'both'
            elif nick_blocked:
                block_type = 'nick'
            else:
                block_type = 'host'
            LOGGER.info(
                "[%s]%s prevented from using %s.",
                block_type,
                nick,
                ', '.join(list_of_blocked_functions)
            )

    def on_scheduler_error(self, scheduler, exc):
        """Called when the Job Scheduler fails.

        .. seealso::

            :meth:`error`
        """
        self.error(exception=exc)

    def on_job_error(self, scheduler, job, exc):
        """Called when a job from the Job Scheduler fails.

        .. seealso::

            :meth:`error`
        """
        self.error(exception=exc)

    def error(self, trigger=None, exception=None):
        """Called internally when a plugin causes an error."""
        message = 'Unexpected error'
        if exception:
            message = '{} ({})'.format(message, exception)

        if trigger:
            message = '{} from {} at {}. Message was: {}'.format(
                message, trigger.nick, str(datetime.now()), trigger.group(0)
            )

        LOGGER.exception(message)

        if trigger and self.settings.core.reply_errors and trigger.sender is not None:
            self.say(message, trigger.sender)

    def _host_blocked(self, host):
        bad_masks = self.config.core.host_blocks
        for bad_mask in bad_masks:
            bad_mask = bad_mask.strip()
            if not bad_mask:
                continue
            if (re.match(bad_mask + '$', host, re.IGNORECASE) or
                    bad_mask == host):
                return True
        return False

    def _nick_blocked(self, nick):
        bad_nicks = self.config.core.nick_blocks
        for bad_nick in bad_nicks:
            bad_nick = bad_nick.strip()
            if not bad_nick:
                continue
            if (re.match(bad_nick + '$', nick, re.IGNORECASE) or
                    Identifier(bad_nick) == nick):
                return True
        return False

    def _shutdown(self):
        # Stop Job Scheduler
        LOGGER.info('Stopping the Job Scheduler.')
        self.scheduler.stop()

        try:
            self.scheduler.join(timeout=15)
        except RuntimeError:
            LOGGER.exception('Unable to stop the Job Scheduler.')
        else:
            LOGGER.info('Job Scheduler stopped.')

        self.scheduler.clear_jobs()

        # Shutdown plugins
        LOGGER.info(
            'Calling shutdown for %d modules.', len(self.shutdown_methods))

        for shutdown_method in self.shutdown_methods:
            try:
                LOGGER.debug(
                    'Calling %s.%s',
                    shutdown_method.__module__,
                    shutdown_method.__name__)
                shutdown_method(self)
            except Exception as e:
                LOGGER.exception('Error calling shutdown method: %s', e)

        # Avoid calling shutdown methods if we already have.
        self.shutdown_methods = []

    def register_url_callback(self, pattern, callback):
        """Register a ``callback`` for URLs matching the regex ``pattern``.

        :param pattern: compiled regex pattern to register
        :type pattern: :ref:`re.Pattern <python:re-objects>`
        :param callback: callable object to handle matching URLs
        :type callback: :term:`function`

        .. versionadded:: 7.0

            This method replaces manual management of ``url_callbacks`` in
            Sopel's plugins, so instead of doing this in ``setup()``::

                if 'url_callbacks' not in bot.memory:
                    bot.memory['url_callbacks'] = tools.SopelMemory()

                regex = re.compile(r'http://example.com/path/.*')
                bot.memory['url_callbacks'][regex] = callback

            use this much more concise pattern::

                regex = re.compile(r'http://example.com/path/.*')
                bot.register_url_callback(regex, callback)

        """
        if 'url_callbacks' not in self.memory:
            self.memory['url_callbacks'] = tools.SopelMemory()

        if isinstance(pattern, basestring):
            pattern = re.compile(pattern)

        self.memory['url_callbacks'][pattern] = callback

    def unregister_url_callback(self, pattern):
        """Unregister the callback for URLs matching the regex ``pattern``.

        :param pattern: compiled regex pattern to unregister callback
        :type pattern: :ref:`re.Pattern <python:re-objects>`

        .. versionadded:: 7.0

            This method replaces manual management of ``url_callbacks`` in
            Sopel's plugins, so instead of doing this in ``shutdown()``::

                regex = re.compile(r'http://example.com/path/.*')
                try:
                    del bot.memory['url_callbacks'][regex]
                except KeyError:
                    pass

            use this much more concise pattern::

                regex = re.compile(r'http://example.com/path/.*')
                bot.unregister_url_callback(regex)

        """
        if 'url_callbacks' not in self.memory:
            # nothing to unregister
            return

        if isinstance(pattern, basestring):
            pattern = re.compile(pattern)

        try:
            del self.memory['url_callbacks'][pattern]
        except KeyError:
            pass

    def search_url_callbacks(self, url):
        """Yield callbacks found for ``url`` matching their regex pattern.

        :param str url: URL found in a trigger
        :return: yield 2-value tuples of ``(callback, match)``

        For each pattern that matches the ``url`` parameter, it yields a
        2-value tuple of ``(callable, match)`` for that pattern.

        The ``callable`` is the one registered with
        :meth:`register_url_callback`, and the ``match`` is the result of
        the regex pattern's ``search`` method.

        .. versionadded:: 7.0

        .. seealso::

            The Python documentation for the `re.search`__ function and
            the `match object`__.

        .. __: https://docs.python.org/3.6/library/re.html#re.search
        .. __: https://docs.python.org/3.6/library/re.html#match-objects

        """
        if 'url_callbacks' not in self.memory:
            # nothing to search
            return

        for regex, function in tools.iteritems(self.memory['url_callbacks']):
            match = regex.search(url)
            if match:
                yield function, match

    def restart(self, message):
        """Disconnect from IRC and restart the bot."""
        self.wantsrestart = True
        self.quit(message)


class SopelWrapper(object):
    """Wrapper around a Sopel instance and a Trigger

    :param sopel: Sopel instance
    :type sopel: :class:`~sopel.bot.Sopel`
    :param trigger: IRC Trigger line
    :type trigger: :class:`sopel.trigger.Trigger`

    This wrapper will be used to call Sopel's triggered commands and rules as
    their ``bot`` argument. It acts as a proxy to :meth:`send messages<say>` to
    the sender (either a channel or in a private message) and even to
    :meth:`reply to someone<reply>` in a channel.
    """
    def __init__(self, sopel, trigger):
        # The custom __setattr__ for this class sets the attribute on the
        # original bot object. We don't want that for these, so we set them
        # with the normal __setattr__.
        object.__setattr__(self, '_bot', sopel)
        object.__setattr__(self, '_trigger', trigger)

    def __dir__(self):
        classattrs = [attr for attr in self.__class__.__dict__
                      if not attr.startswith('__')]
        return list(self.__dict__) + classattrs + dir(self._bot)

    def __getattr__(self, attr):
        return getattr(self._bot, attr)

    def __setattr__(self, attr, value):
        return setattr(self._bot, attr, value)

    def say(self, message, destination=None, max_messages=1):
        """Override ``Sopel.say`` to send message to sender

        :param str message: message to say
        :param str destination: channel or person; defaults to trigger's sender
        :param int max_messages: max number of message splits

        The ``destination`` will default to the channel in which the
        trigger happened (or nickname, if received in a private message).

        .. seealso::

            :meth:`sopel.bot.Sopel.say`
        """
        if destination is None:
            destination = self._trigger.sender
        self._bot.say(message, destination, max_messages)

    def action(self, message, destination=None):
        """Override ``Sopel.action`` to send action to sender

        :param str message: action message
        :param str destination: channel or person; defaults to trigger's sender

        The ``destination`` will default to the channel in which the
        trigger happened (or nickname, if received in a private message).

        .. seealso::

            :meth:`sopel.bot.Sopel.action`
        """
        if destination is None:
            destination = self._trigger.sender
        self._bot.action(message, destination)

    def notice(self, message, destination=None):
        """Override ``Sopel.notice`` to send a notice to sender

        :param str message: notice message
        :param str destination: channel or person; defaults to trigger's sender

        The ``destination`` will default to the channel in which the
        trigger happened (or nickname, if received in a private message).

        .. seealso::

            :meth:`sopel.bot.Sopel.notice`
        """
        if destination is None:
            destination = self._trigger.sender
        self._bot.notice(message, destination)

    def reply(self, message, destination=None, reply_to=None, notice=False):
        """Override ``Sopel.reply`` to reply to someone

        :param str message: reply message
        :param str destination: channel or person; defaults to trigger's sender
        :param str reply_to: person to reply to; defaults to trigger's nick
        :param bool notice: reply as an IRC notice or with a simple message

        The ``destination`` will default to the channel in which the
        trigger happened (or nickname, if received in a private message).

        ``reply_to`` will default to the nickname who sent the trigger.

        .. seealso::

            :meth:`sopel.bot.Sopel.reply`
        """
        if destination is None:
            destination = self._trigger.sender
        if reply_to is None:
            reply_to = self._trigger.nick
        self._bot.reply(message, destination, reply_to, notice)

    def kick(self, nick, channel=None, message=None):
        """Override ``Sopel.kick`` to kick in a channel

        :param str nick: nick to kick out of the ``channel``
        :param str channel: optional channel to kick ``nick`` from
        :param str message: optional message for the kick

        The ``channel`` will default to the channel in which the call was
        triggered. If triggered from a private message, ``channel`` is
        required.

        .. seealso::

            :meth:`sopel.bot.Sopel.kick`
        """
        if channel is None:
            if self._trigger.is_privmsg:
                raise RuntimeError('Error: KICK requires a channel.')
            else:
                channel = self._trigger.sender
        if nick is None:
            raise RuntimeError('Error: KICK requires a nick.')
        self._bot.kick(nick, channel, message)

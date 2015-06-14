#!/usr/bin/env python2

# Python
from BaseHTTPServer import HTTPServer
import abc
import argparse
import codecs
import logging
import os
import select
import socket
import sys

# 3rd party
from inotify.watcher import Watcher
from prometheus_client import Counter
from prometheus_client import MetricsHandler
import inotify

# TODO: Implement offline mode
# TODO: Support other inotify modules?
# TODO: Support Python3 (using inotifyx?)

logger = logging.getLogger(__name__)


POLL_TIMEOUT = 10000
FILE_EVENTS_TO_WATCH = inotify.IN_MODIFY
DIR_EVENTS_TO_WATCH = inotify.IN_MOVED_TO | inotify.IN_MOVED_FROM | inotify.IN_DELETE | inotify.IN_CREATE


class MetaAbstractLineHandler(abc.ABCMeta):

    '''Keep track of all subclassed versions'''

    children = []

    def __init__(cls, name, bases, dct):
        cls.children.append(cls)
        super(MetaAbstractLineHandler, cls).__init__(name, bases, dct)


# pylint: disable=R0921
class AbstractLineHandler(object):
    '''Base class for building your own LineHandler

    After subclassing implement your own process method'''

    __metaclass__ = MetaAbstractLineHandler

    testcases = None  # None = throw warning; False = no testcases; otherwise iterable with testcases
    testcase_args = None  # Used to instantiate this class for a testcase
    testcase_kwargs = None  # Used to instantiate this class for a testcase

    @abc.abstractmethod
    def process(self, line):
        pass

    @property
    def logger(self):
        try:
            return self._logger
        except AttributeError:
            self._logger = logging.getLogger(__name__ + '.' + self.__class__.__name__)
            return self._logger


class FileStats(object):
    '''Track handlers for a spefic file'''

    def __init__(self, handlers):
        self.watchdescriptor = None
        self._filehandle = None
        self.position_in_file = None
        self.unprocessed = ''
        self.handlers = handlers

    def __repr__(self):
        return '{}(handle={}, position={}, handlers={})'.format(self.__class__.__name__, self._filehandle, self.position_in_file, self.handlers)

    @property
    def filehandle(self):
        return self._filehandle

    @filehandle.setter
    def filehandle(self, handle):
        self._filehandle = handle
        if handle is None:
            self.position_in_file = -1
        else:
            self.position_in_file = handle.tell()

    def __del__(self):
        self.disable()

    def disable(self):
        try:
            self._filehandle.close()
        except (IOError, AttributeError):
            pass
        self.watchdescriptor = None
        self._filehandle = None
        self.unprocessed = ''


class CloudedEvent(inotify.watcher.Event):
    '''Wrapper class to protect against segfaults.

    python-inotify can segfault when requesting the __repr__ of certain events.
    To prevent this we will override this method with a less useful but also
    non-crashing version.

    https://bitbucket.org/JanKanis/python-inotify/issue/5/possible-segfault-in-_inotifyc-read_events
    https://bitbucket.org/JanKanis/python-inotify/issue/8/segfault-watching-directory
    https://bitbucket.org/JanKanis/python-inotify/issue/10/str-or-repr-of-event-results-in-core-dump
    '''

    def __repr__(self):
        return 'CloudedEvent(wd={0.wd}, fullpath={0.fullpath}, mask={0.mask}, cookie={0.cookie})'.format(self)


class DirStats(object):

    def __init__(self, filenames):
        self.filenames = filenames

    def __repr__(self):
        return '{}(filenames={})'.format(self.__class__.__name__, self.filenames)


def ignore_untracked(func):
    def wrapped(self, event, *args, **kwargs):
        if event.fullpath not in self.filestats:
            logger.debug('Ignoring unknown file %s.', event.fullpath)
            return
        return func(self, event, *args, **kwargs)
    return wrapped


class MyWatcher(Watcher):
    '''An inotify watcher meant for tracking log files

    This watcher has the following characteristics:

    - A file that has multiple handlers will only be read once per change
    - When a file is replaced the watcher will switch to the new file
    - A file can be created after the watcher got started and it will still be processed

    When files have new content the appropriate handlers will be called to process it.
    '''

    # Copied from inotify/watcher.py
    _event_props = {
        'access': 'File was accessed',
        'modify': 'File was modified',
        'attrib': 'Attribute of a directory entry was changed',
        'close_write': 'File was closed after being written to',
        'close_nowrite': 'File was closed without being written to',
        'open': 'File was opened',
        'moved_from': 'Directory entry was renamed from this name',
        'moved_to': 'Directory entry was renamed to this name',
        'create': 'Directory entry was created',
        'delete': 'Directory entry was deleted',
        'delete_self': 'The watched directory entry was deleted',
        'move_self': 'The watched directory entry was renamed',
        'unmount': 'Directory was unmounted, and can no longer be watched',
        'q_overflow': 'Kernel dropped events due to queue overflow',
        'ignored': 'Directory entry is no longer being watched',
        'isdir': 'Event occurred on a directory',
    }

    def __init__(self, *args, **kwargs):
        super(MyWatcher, self).__init__(*args, **kwargs)
        self.filestats = {}
        self.dirstats = {}

    def add_handler(self, path, handler):
        try:
            self.filestats[path].handlers.append(handler)
        except KeyError:
            self.filestats[path] = FileStats([handler])
        self.add(path)

    def add(self, path, from_beginning_of_file=False):
        # Registering a handler on the file itself
        filestats = self.filestats[path]

        if filestats.watchdescriptor is None:
            try:
                filestats.watchdescriptor = super(MyWatcher, self).add(path, FILE_EVENTS_TO_WATCH)
            except OSError as ex:
                logger.info('Non-fatal problem: failed to open %s: %s', path, ex)
            self.reset_filehandle(path, from_beginning_of_file)

        # Registering a handler on the folder that contains the file, to detect file renames
        dirname = os.path.dirname(path)
        try:
            self.dirstats[dirname].filenames.append(path)
        except KeyError:
            super(MyWatcher, self).add(dirname, DIR_EVENTS_TO_WATCH)
            self.dirstats[dirname] = DirStats([path])

    def reset_filehandle(self, path, from_beginning_of_file=False):
        stats = self.filestats[path]

        # Cleanup
        if stats.filehandle:
            try:
                stats.filehandle.close()
            except IOError as ex:
                logger.info('Failed to close filehandle %s: %s', path, ex)

        # Setup
        try:
            # Opening an unbuffered stream, requires since we use select
            handle = codecs.open(path, 'r', 'UTF-8', 'replace', 0)
            if from_beginning_of_file:
                handle.seek(0)
            else:
                handle.seek(0, 2)  # 0 bytes from the end of the file
        except IOError:
            # This can happen when the file doesn't exist yet.
            handle = None

        stats.filehandle = handle
        stats.unprocessed = ''

    def read(self, bufsize=None):
        return [CloudedEvent(event.raw, event.path) for event in super(MyWatcher, self).read(bufsize)]

    def process_events(self, bufsize=None):
        events = self.read(bufsize)
        for event in events:
            for event_type in self._event_props:
                if getattr(event, event_type):
                    try:
                        handler = getattr(self, 'process_' + event_type)
                    except AttributeError:
                        logger.debug('No handler for %s', event_type)
                    else:
                        logger.debug('Calling handler for %s', event_type)
                        handler(event)

    @ignore_untracked
    def process_moved_from(self, event):
        logger.debug('DELETE/MOVED_FROM Event: %s', event.fullpath)
        logger.debug('Removing inotify from %s', event.fullpath)
        try:
            self.remove_path(event.fullpath)  # Stop monitoring with inotify
        except inotify.watcher.InotifyWatcherException:
            # Apparently we weren't even watching that file
            self.process_ignored(event)

    process_delete = process_moved_from

    @ignore_untracked
    def process_moved_to(self, event):
        logger.debug('MOVED_TO Event: %s', event.fullpath)
        logger.debug('Adding inotify to %s', event.fullpath)
        self.add(event.fullpath)  # (re)start monitoring with inotify

    @ignore_untracked
    def process_create(self, event):
        logger.debug('CREATE Event: %s', event.fullpath)
        logger.debug('Adding inotify to %s', event.fullpath)
        self.add(event.fullpath, from_beginning_of_file=True)  # (re)start monitoring with inotify
        self.process_modify(event)

    def process_modify(self, event):
        filestats = self.filestats[event.fullpath]

        if filestats.filehandle is None:
            logger.debug('Ignoring read for non-existent file %s', event.fullpath)
            return

        # first, check if the file was truncated:
        curr_size = os.fstat(filestats.filehandle.fileno()).st_size
        if curr_size < filestats.position_in_file:
            logger.info('File %s was truncated, seeking to beginning of file', event.fullpath)
            filestats.filehandle.seek(0)
            filestats.position_in_file = 0
            filestats.unprocessed = ''

        try:
            partial = filestats.filehandle.read()
            try:
                last_newline = partial.rindex('\n')
            except ValueError:
                if partial:
                    logger.debug('No newline found: %s', repr(partial))
                lines = []
                filestats.unprocessed += partial
            else:
                lines = (filestats.unprocessed + partial[:last_newline]).splitlines()
                filestats.unprocessed = partial[(last_newline + 1):]  # +1 because we don't care about the newline
            filestats.position_in_file = filestats.filehandle.tell()
        except IOError:
            logger.warning('Error reading lines from file %s', event.fullpath)
            return

        for line in lines:
            # logger.debug('%s: %s', event.fullpath, line)

            for handler in filestats.handlers:
                try:
                    handler.process(line)
                except Exception:
                    # Catching all possible exceptions: Continued service is
                    # more important than the processing of a particular line
                    handler.logger.exception('Failed to process line %s', repr(line))

    def process_ignored(self, event):
        logger.debug('inotify reported it is no longer monitoring %s', event.fullpath)
        try:
            filestats = self.filestats[event.fullpath]
        except KeyError:
            logger.debug('inotify reported it is no longer monitoring unknown %s', event.fullpath)
        finally:
            filestats.disable()
            # If the path was in self.filestats we're interested in it...
            if os.path.exists(event.fullpath):
                logger.debug('inotify reported its no longer monitoring %s, readding it.', event.fullpath)
                self.add(event.fullpath)
            else:
                logger.debug('inotify reported its no longer monitoring %s.', event.fullpath)


class MoreSilentMetricsHandler(MetricsHandler):
    '''A more silent version of the vanilla MetricsHandler'''

    def log_request(self, code='-', *args, **kwargs):
        if code == 200:
            return
        # Old-style class, so no super()
        MetricsHandler.log_request(self, code, *args, **kwargs)


class MoreRobustHTTPServer(HTTPServer):
    '''A more robust version of the vanilla HTTPServer

    Unlike the vanilla vesion this won't stop functioning once a broken pipe is
    encoutered.'''

    def _handle_request_noblock(self):
        try:
            # No super as HTTPServer is an old-style class
            HTTPServer._handle_request_noblock(self)
        except socket.error:
            logger.info('Socket error.')


def start_http_server(portnr):
    server_address = ('', portnr)
    httpd = MoreRobustHTTPServer(server_address, MoreSilentMetricsHandler)
    return httpd


def run_offline(setting, logfiles):
    raise NotImplementedError()


def run_online(settings, logfiles):

    READ_ONLY = select.POLLIN | select.POLLPRI | select.POLLHUP | select.POLLERR
    # READ_WRITE = READ_ONLY | select.POLLOUT

    poller = select.poll()

    http_server = start_http_server(settings.port)
    logger.info('Now listening for HTTP requests on port %s', settings.port)
    poller.register(http_server, READ_ONLY)

    filesystem_server = MyWatcher()
    poller.register(filesystem_server, READ_ONLY)

    for (filename, handler) in logfiles:
        filesystem_server.add_handler(filename, handler)

    pollcount = Counter('pollcount', 'The number of poll events processed by logfile_exporter.')  # noqa

    loopcount = 0
    while settings.max_polls <= 0 or loopcount < settings.max_polls:
        events = poller.poll(POLL_TIMEOUT)
        pollcount.inc()
        loopcount += 1

        for fd, _event in events:
            if fd == http_server.fileno():
                http_server._handle_request_noblock()
            elif fd == filesystem_server.fileno():
                filesystem_server.process_events()
            else:
                logger.warning('Event from an unknown file descriptor')

    logger.info('Terminating program.')


def run_testcases(handlers):
    import unittest
    from tests import load_tests_from_handler

    # Removing duplicate handlers
    unique_handlers = set([type(handler) for handler in handlers])
    logger.info('Running testcases')

    failures = 0
    errors = 0

    for handler_type in unique_handlers:
        tests = load_tests_from_handler(unittest.defaultTestLoader, handler_type)
        if tests:
            result = tests(unittest.result.TestResult())
            failure_count = len(result.failures)
            error_count = len(result.errors)

            failures += failure_count
            errors += error_count

            logger_func = logger.info
            if failure_count or error_count:
                logger_func = logger.warning

            logger_func(
                '%s executed %s testcases: %s failures, %s errors.',
                handler_type,
                result.testsRun,
                failure_count,
                error_count,
            )

        else:
            if handler_type.testcases is False:
                logger.info('%s has no testcases.', handler_type)
            else:
                logger.warning('%s has no testcases.', handler_type)

    return (failures, errors)


def run(myfiles, configure_basic_logger=True):

    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose', action='count', default=0)
    parser.add_argument('-q', '--quiet', action='count', default=0)
    parser.add_argument('-p', '--port', default=9123, type=int, help='Port to listen on')
    parser.add_argument('-o', '--offline', action='store_true', help='Feed the existing log files to the handlers and then quit.')
    parser.add_argument('-t', '--testcases', choices=['skip', 'strict', 'run', 'run-then-quit'], default='run')
    parser.add_argument('--max-polls', default=-1, type=int, help=argparse.SUPPRESS)

    args = parser.parse_args()

    if configure_basic_logger:
        desired_loglevel = max(1, logging.INFO - (args.verbose * 10) + (args.quiet * 10))
        logging.basicConfig(
            level=desired_loglevel,
            datefmt='%Y-%m-%d %H:%M:%S',
            format='%(asctime)s %(levelname)-10s [%(name)s] %(message)s',
        )

    if args.testcases in ['strict', 'run', 'run-then-quit']:
        (failures, errors) = run_testcases([handler for (_filename, handler) in myfiles])

        if args.testcases == 'run-then-quit':
            exit_code = 0 if max(failures, errors) == 0 else 9
            sys.exit(exit_code)
        if args.testcases == 'strict' and max(failures, errors) > 0:
            logger.error('Aborting program; not all testcases passed.')
            sys.exit(9)

    if args.offline:
        run_offline(args, myfiles)
    else:
        try:
            run_online(args, myfiles)
        except KeyboardInterrupt:
            pass

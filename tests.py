#!/usr/bin/env python2

# Python
from os.path import join
import imp
import logging
import os
import select
import shutil
import tempfile
import unittest

# 3rd part
import prometheus_client

# Local
from logfile_exporter import AbstractLineHandler
from logfile_exporter import MetaAbstractLineHandler
from logfile_exporter import MyWatcher


logger = logging.getLogger('logfile_exporter.tests')


class RecordingAbstractLineHandler(AbstractLineHandler):

    '''LineHandler that keeps track of all process() calls'''

    testcases = False

    def __init__(self, *args, **kwargs):
        super(RecordingAbstractLineHandler, self).__init__(*args, **kwargs)
        self.lines = []

    def process(self, line):
        self.lines.append(line)


def noop_collect(*args, **kwargs):
    return []


class BaseTestLineHandler(unittest.TestCase):
    def setUp(self):
        # Disabling auto collectors
        self._PROCESS_COLLECTOR_collect = prometheus_client.PROCESS_COLLECTOR.collect
        prometheus_client.PROCESS_COLLECTOR.collect = noop_collect

    def tearDown(self):
        # Resetting all metrics
        for metric in prometheus_client.REGISTRY._collectors:
            try:
                type_ = metric._type
            except AttributeError:
                # Special metrics such as ProcessCollector. Should be dealt
                # with on a case-by-base basis
                continue

            if type_ == 'counter':
                with metric._lock:
                    metric._metrics = {}
            elif type_ == 'gauge':
                with metric._lock:
                    metric._metrics = {}
            else:
                logger.warning('Failed to reset %s.%s: unknown Prometheus type %s', handler, var, obj._type)

        # Re-enabling auto collectors
        prometheus_client.PROCESS_COLLECTOR.collect = self._PROCESS_COLLECTOR_collect

    def _test(self, testcase):
        for line in testcase['input'].splitlines():
            self.instance.process(line)

        result = []
        for metric in prometheus_client.REGISTRY.collect():
            for (name, tags, value) in metric._samples:
                result.append((
                    name,
                    sorted(tuple(tags.iteritems())),
                    value,
                ))
        result.sort()

        expected = testcase.get('expected', [])
        for (name, tags, value) in expected:
            tags.sort()
        expected.sort()

        self.assertEqual(expected, result)


def load_tests_from_handler(loader, handler):
    '''Return all testcases for a handler'''

    if handler.testcases is None:
        if handler != AbstractLineHandler:
            logger.warning('Handler %s has no testcases.', handler)
        return []

    if handler.testcases is False:
        return []

    # New class per handler
    class Derived(BaseTestLineHandler):
        pass
    # Instantiating handler
    args = []
    if handler.testcase_args is not None:
        args = handler.testcase_args
    kwargs = {}
    if handler.testcase_kwargs is not None:
        kwargs = handler.testcase_kwargs
    Derived.instance = handler(*args, **kwargs)

    # Setting up testcases
    for (index, testcase) in enumerate(handler.testcases, start=1):
        method_name = 'test_testcase_{}'.format(index)
        setattr(Derived, method_name, lambda self, testcase=testcase: self._test(testcase))

    tests = loader.loadTestsFromTestCase(Derived)

    return tests


def load_tests(loader, standard_tests, _pattern):
    suite = unittest.TestSuite()

    suite.addTests(standard_tests)

    # Loading all available programs to allow subclasses of AbstractLineHandler
    # to register themselves with MetaAbstractLineHandler
    for filename in os.listdir(os.path.dirname(os.path.abspath(__file__))):
        (filename_base, _sep, extention) = filename.rpartition('.')
        if filename_base.startswith('program_') and extention == 'py':
            imp.load_source(filename_base, filename)

    for handler in MetaAbstractLineHandler.children:
        suite.addTests(load_tests_from_handler(loader, handler))

    return suite


class TestWatcher(unittest.TestCase):

    '''Tests for an inotify watcher.

    Inotify is based on inodes, but as a user we're only interested in
    filepahs. while the inode stays alive its path can change.  The reverse
    also happens, the underlying inode of a path can change.

    These tests should validate that the watcher is working on the filepaths,
    not on the iddes.
    '''

    POLL_TIMEOUT = 10  # in ms

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.poller = select.poll()
        self.watcher = MyWatcher()
        self.recorder = RecordingAbstractLineHandler()
        self.poller.register(self.watcher, select.POLLIN | select.POLLPRI | select.POLLHUP | select.POLLERR)

    def tearDown(self):
        shutil.rmtree(self.folder)

    def poll(self):

        while self.poller.poll(self.POLL_TIMEOUT):
            self.watcher.process_events()

    def test_python_inotify_segfault_protection(self):
        # python-inotify segfaults when you print certain events

        syslog = join(self.folder, 'syslog')
        self.watcher.add_handler(syslog, self.recorder)

        syslog = join(self.folder, 'syslog')
        with open(syslog, 'w') as handle:
            handle.write('12:34 First entry\n')

        shutil.move(syslog, syslog + '.1')
        os.unlink(syslog + '.1')

        while self.poller.poll(self.POLL_TIMEOUT):
            events = self.watcher.read()
            for event in events:
                repr(event)

    def test_read_on_existing_file(self):
        syslog = join(self.folder, 'syslog')
        with open(syslog, 'w') as handle:

            handle.write('12:34 First entry\n')
            handle.flush()

            self.poll()

            self.watcher.add_handler(syslog, self.recorder)

            handle.write('12:35 Second entry\n')
            handle.flush()

            self.poll()

            self.assertEqual(self.recorder.lines, ['12:35 Second entry'])

    def test_read_on_created_file(self):
        syslog = join(self.folder, 'syslog')
        self.watcher.add_handler(syslog, self.recorder)

        events = self.poller.poll(self.POLL_TIMEOUT)
        self.assertEqual(events, [])

        with open(syslog, 'w') as handle:

            handle.write('12:34 First entry\n')
            handle.flush()

            handle.write('12:35 Second entry\n')
            handle.flush()

            self.poll()

            self.assertEqual(self.recorder.lines, ['12:34 First entry', '12:35 Second entry'])

            handle.write('12:36 Third entry\n')
            handle.flush()

            self.poll()

            self.assertEqual(self.recorder.lines, ['12:34 First entry', '12:35 Second entry', '12:36 Third entry'])

    def test_read_on_recreated_file_after_delete(self):
        syslog = join(self.folder, 'syslog')
        with open(syslog, 'w') as handle:
            handle.write('12:34 First entry\n')
            handle.flush()

        self.watcher.add_handler(syslog, self.recorder)

        events = self.poller.poll(self.POLL_TIMEOUT)
        self.assertEqual(events, [])

        with open(syslog, 'a') as handle:

            handle.write('12:35 Second entry\n')
            handle.flush()

            self.poll()

            self.assertEqual(self.recorder.lines, ['12:35 Second entry'])

        os.unlink(syslog)
        self.poll()

        with open(syslog, 'w') as handle:
            handle.write('12:36 Third entry\n')
            handle.flush()

            self.poll()

            self.assertEqual(self.recorder.lines, ['12:35 Second entry', '12:36 Third entry'])

    def test_read_on_recreated_file_after_move(self):
        syslog = join(self.folder, 'syslog')
        with open(syslog, 'w') as handle:
            handle.write('12:34 First entry\n')
            handle.flush()

        self.watcher.add_handler(syslog, self.recorder)

        events = self.poller.poll(self.POLL_TIMEOUT)
        self.assertEqual(events, [])

        with open(syslog, 'a') as handle:

            handle.write('12:35 Second entry\n')
            handle.flush()

            self.poll()

            self.assertEqual(self.recorder.lines, ['12:35 Second entry'])

        shutil.move(syslog, syslog + '.1')
        self.poll()

        with open(syslog, 'w') as handle:
            handle.write('12:36 Third entry\n')
            handle.flush()

            self.poll()

            self.assertEqual(self.recorder.lines, ['12:35 Second entry', '12:36 Third entry'])

    def test_nonread_on_moved_file(self):
        syslog = join(self.folder, 'syslog')
        syslog1 = syslog + '.1'

        with open(syslog, 'w') as handle:
            handle.write('12:34 First entry\n')
            handle.flush()

        self.watcher.add_handler(syslog, self.recorder)

        events = self.poller.poll(self.POLL_TIMEOUT)
        self.assertEqual(events, [])

        with open(syslog, 'a') as handle:

            handle.write('12:35 Second entry\n')
            handle.flush()

            self.poll()

            self.assertEqual(self.recorder.lines, ['12:35 Second entry'])

            shutil.move(syslog, syslog1)
            self.poll()

            handle.write('12:36 Third entry\n')
            handle.flush()

            self.poll()

            self.assertEqual(self.recorder.lines, ['12:35 Second entry'])

        with open(syslog, 'a') as handle:

            handle.write('12:37 Fourth entry\n')
            handle.flush()

            self.poll()

            self.assertEqual(self.recorder.lines, ['12:35 Second entry', '12:37 Fourth entry'])

    def test_read_on_moved_in_file(self):
        syslog = join(self.folder, 'syslog')
        syslog1 = syslog + '.1'

        with open(syslog, 'w') as handle:
            handle.write('12:34 First entry\n')
            handle.flush()

        self.watcher.add_handler(syslog, self.recorder)

        events = self.poller.poll(self.POLL_TIMEOUT)
        self.assertEqual(events, [])

        with open(syslog, 'a') as handle:

            handle.write('12:35 Second entry\n')
            handle.flush()

            self.poll()
            self.assertEqual(self.recorder.lines, ['12:35 Second entry'])

        shutil.move(syslog, syslog1)
        self.poll()
        self.assertEqual(self.recorder.lines, ['12:35 Second entry'])

        with open(syslog1, 'a') as handle:

            handle.write('12:36 Third entry\n')
            handle.flush()

            self.poll()
            self.assertEqual(self.recorder.lines, ['12:35 Second entry'])

            shutil.move(syslog1, syslog)
            self.poll()

            handle.write('12:37 Fourth entry\n')
            handle.flush()

            self.poll()

            self.assertEqual(self.recorder.lines, ['12:35 Second entry', '12:37 Fourth entry'])

    def test_ignore_untracked(self):
        syslog = join(self.folder, 'syslog')
        self.watcher.add_handler(syslog, self.recorder)

        messageslog = join(self.folder, 'messages')

        with open(syslog, 'w') as handle:

            handle.write('12:34 First entry\n12:35 Second entry\n')
            handle.flush()

        with open(messageslog, 'w') as handle:

            handle.write('12:36 Third entry\n')
            handle.flush()

        self.poll()

        self.assertEqual(self.recorder.lines, ['12:34 First entry', '12:35 Second entry'])


if __name__ == '__main__':
    logging.basicConfig(
        datefmt='%Y-%m-%d %H:%M:%S',
        format='%(asctime)s %(levelname)-10s [%(name)s] %(message)s',
    )
    unittest.main()

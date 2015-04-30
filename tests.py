#!/usr/bin/env python2

# Python
from os.path import join
import os
import select
import shutil
import tempfile
import unittest

# Local
from logfile_exporter import AbstractLineHandler
from logfile_exporter import MyWatcher


class RecordingAbstractLineHandler(AbstractLineHandler):

    def __init__(self, *args, **kwargs):
        super(RecordingAbstractLineHandler, self).__init__(*args, **kwargs)
        self.lines = []

    def process(self, line):
        self.lines.append(line)


class TestWatcher(unittest.TestCase):

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

        with open(syslog1, 'a') as handle:

            handle.write('12:36 Third entry\n')
            handle.flush()

            self.poll()


            self.assertEqual(self.recorder.lines, ['12:35 Second entry'])

        shutil.move(syslog1, syslog)
        self.poll()

        with open(syslog, 'a') as handle:

            handle.write('12:37 Fourth entry\n')
            handle.flush()

            self.poll()

            self.assertEqual(self.recorder.lines, ['12:35 Second entry', '12:37 Fourth entry'])


if __name__ == '__main__':
    unittest.main()

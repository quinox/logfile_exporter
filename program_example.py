#!/usr/bin/env python2

# 3rd party imports
from prometheus_client import Counter

# Local imports
from logfile_exporter import run
from logfile_exporter import AbstractLineHandler


class LineCounter(AbstractLineHandler):
    '''Example LineHandler that counts every line

    This handler will count every line in the logfile. In your own
    implementation you might want to only count lines that contain the word
    'ERROR', 'Failed login', 'Critial' etc.

    Always define counters on class level, creating them on instance level can
    result in weird behaviour because running testcases will make new classes.
    '''

    linecounter = Counter('linecount', 'Nr. of analyzed lines', ['filename'])

    testcases = [
        {
            'input': '''Line one
Line two
Line three''',
            'expected': [
                ('linecount', [('filename', '/some/file')], 3),
            ]
        }
    ]
    testcase_args = ['/some/file']

    def __init__(self, filename):
        self.filename = filename
        super(LineCounter, self).__init__()

    def process(self, line):
        # This is the part that you should modify to get your own behaviour
        # For now we'll simply increase the counter for every line processed
        self.linecounter.labels(self.filename).inc()


class LetterCounter(AbstractLineHandler):
    '''Example LineHandler that counts the number of letters'''

    lettercounter = Counter('lettercount', 'Nr. of letters in the log files', ['filename', 'lettertype'])

    testcases = [
        {
            'input': '''
12:34 Some log message
12:35 There were 6 missed calls
            ''',
            'expected': [
                ('lettercount', [('filename', '/var/log/file'), ('lettertype', 'lower')], 32.0),
                ('lettercount', [('filename', '/var/log/file'), ('lettertype', 'upper')], 2.0),
            ]
        }
    ]
    testcase_args = ['/var/log/file']

    def __init__(self, filename):
        self.filename = filename
        super(LetterCounter, self).__init__()

    def process(self, line):
        self.lettercounter.labels(self.filename, 'upper').inc(len([x for x in line if x.isupper()]))
        self.lettercounter.labels(self.filename, 'lower').inc(len([x for x in line if x.islower()]))


class PrintingLineHandler(AbstractLineHandler):
    '''Example LineHandler that prints all log lines

    This class doesn't set any Prometheus metrics; normally you should (why
    else use this script?)'''

    testcases = False  # Since we don't do anything with Prometheus we'll skip the testcases

    max_line_length = 100

    def __init__(self, filename):
        self.filename = filename
        super(PrintingLineHandler, self).__init__()
        # Use self.logger to log messages:
        self.logger.info('Printing all lines from %s on stdout.', filename)

    def process(self, line):
        if len(line) > 100:
            print('... ' + line[:self.max_line_length - 4] + ' ...')
        else:
            print('... ' + line)


if __name__ == '__main__':

    run([
        ('/var/log/syslog', LineCounter(filename='/var/log/syslog')),
        ('/var/log/syslog', LetterCounter(filename='/var/log/syslog')),
        ('/var/log/syslog', PrintingLineHandler(filename='/var/log/syslog')),
        ('/var/log/auth.log', LineCounter(filename='/var/log/auth.log')),
    ])

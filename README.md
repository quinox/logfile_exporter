# logfile_exporter

This project allows you to easily expose your own logfile statistics for [Prometheus](http://prometheus.io/). Some knowledge of Python is required.

# Seeing it in action

1. Clone the repository
1. Run `run.sh`
1. Visit http://localhost:9123

You should now see a few statistics in your browser; the demo script will count the number of lines inside `/var/log/auth.log` and `/var/log/syslog` and it will count the number of lower and upper case letters inside `/var/log/syslog`.

# Dependencies

* Python 2
* [python-inotify](https://bitbucket.org/JanKanis/python-inotify)

# Customizing

To expose your own logfiles statistics:

1. Copy `program_example.py` to `program_acme.py`
1. Subclass `AbstractLineHandler` as `AcmeLineHandler`
1. Implement `def process(self, line)` inside your new class to extract the statistics you want
1. Add your class to the `run` command at the bottom of the script
1. Activate the virtual in your terminal: `. virtual/bin/activate`
1. Run `python program_acme.py`
1. Visit http://localhost:9123

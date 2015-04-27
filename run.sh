#!/bin/bash -E

PYTHON="/usr/bin/python2"
VIRTUALDIR="virtual"
PROGRAM="program_example.py"

die() {
    echo "$@" >& 2
    exit 1
}

cd "$(dirname "$0")"

echo -e "* Checking virtual...\n"

# The arguments of newer virtualenvs differ from the older versions. In case of
# errors we'll try the older version as well
[ -d "$VIRTUALDIR" ] || virtualenv -p "$PYTHON" "$VIRTUALDIR" --system-site-packages || virtualenv -p "$PYTHON" "$VIRTUALDIR"

. "$VIRTUALDIR/bin/activate"

echo -e "\n\n\n* Checking dependencies...\n" 

pip install -r requirements.txt || die "Could not install all dependenies; scroll up for the cause."

echo -e "\n\n\n* Running $PROGRAM...\n"
python "$PROGRAM" "$@"

#!/bin/bash

set -eo pipefail
if [ "$#" -ne 1 ] || [[ ! "$1" =~ ^[A-Za-z0-9_]+$ ]]; then
    echo 'Provide one PDF set name' >&2
    exit 2
fi

cd '__INS_DIR__/share/LHAPDF'

fetch-source "https://lhapdfsets.web.cern.ch/lhapdfsets/current/$1.tar.gz" "$1.tar.gz"

tar -xzf "$1.tar.gz"

test -s "$1/$1.info"
test -s "$1/${1}_0000.dat"

rm "$1.tar.gz"

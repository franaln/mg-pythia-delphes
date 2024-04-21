#! /bin/bash

cd __INS_DIR__/share/LHAPDF
wget http://lhapdfsets.web.cern.ch/lhapdfsets/current/$1.tar.gz -O $1.tar.gz
tar xvfz $1.tar.gz
rm $1.tar.gz

#! /usr/bin/bash

top_dir=$PWD
pkg_dir=$top_dir/pkgs
tmp_dir=$top_dir/tmp
data_dir=$top_dir/data
# ins_dir=$PWD/mg_pythia_delphes
ins_dir=/mnt/R5/hep_tools/mg_pythia_delphes

CXX=$(command -v g++)
PYTHON=/usr/local/bin/python3.8
# PYTHON_CONFIG=$(find /usr/local/ -iname "python-config.py")
PYTHON_CONFIG=/usr/local/lib/python3.8/config-3.8-x86_64-linux-gnu/python-config.py

export PYTHON=$PYTHON
export PYTHON_CONFIG=$PYTHON_CONFIG
export PYTHON_INCLUDE="-I/usr/local/include/python3.8"

# Versions
ROOT_VERSION=v6.24.06.Linux-centos7-x86_64-gcc4.8
MG_VERSION=3.4.1
MG5aMC_PY8_INTERFACE_VERSION=1.3
HEPMC_VERSION=2.06.09
FASTJET_VERSION=3.3.4
LHAPDF_VERSION=6.5.1
PYTHIA_VERSION=8306
DELPHES_VERSION=3.5.0

# Utils
function download () {
    if [ -f "$1" ] ; then
        echo "File  $1 already downloaded, using local package"
    else
        echo "Downloading $2 to $1"
        wget "$2" -O "$1"
    fi
}

function do_tmp_dir () {
    cd $top_dir
    mkdir -p ${tmp_dir}
    cd $tmp_dir
}

function rm_tmp_dir () {
    cd $top_dir
    rm -rf ${tmp_dir}
}

function dowload_pdf () {
    wget http://lhapdfsets.web.cern.ch/lhapdfsets/current/$1.tar.gz -O $1.tar.gz
    tar xvfz $1.tar.gz
    rm $1.tar.gz
}

function install_setup_root () {
    if [ -d $ins_dir/root ] ; then
        source ${ins_dir}/root/bin/thisroot.sh
    else
        download $pkg_dir/root_${ROOT_VERSION}.tar.gz https://root.cern/download/root_${ROOT_VERSION}.tar.gz
        tar xvfz $pkg_dir/root_${ROOT_VERSION}.tar.gz -C ${ins_dir}
        source ${ins_dir}/root/bin/thisroot.sh
    fi
}

function install_mg () {

    download $pkg_dir/MG5_aMC_v${MG_VERSION}.tar.gz https://launchpad.net/mg5amcnlo/3.0/3.4.x/+download/MG5_aMC_v${MG_VERSION}.tar.gz

    mkdir -p ${ins_dir}/MG5_aMC
    tar -xzvf $pkg_dir/MG5_aMC_v${MG_VERSION}.tar.gz --strip=1 --directory=$ins_dir/MG5_aMC

    # Patch hepmcremove bug
    patch ${ins_dir}/MG5_aMC/madgraph/interface/madevent_interface.py < data/mg_patch_autoremove.patch

    # copy needed python file
    mkdir ${ins_dir}/python
    cp data/six.py ${ins_dir}/python/

}

function install_hepmc () {

    download $pkg_dir/hepmc${HEPMC_VERSION}.tgz http://hepmc.web.cern.ch/hepmc/releases/hepmc${HEPMC_VERSION}.tgz

    do_tmp_dir

    tar xvfz $pkg_dir/hepmc${HEPMC_VERSION}.tgz

    mv hepmc${HEPMC_VERSION} src
    #    mv HepMC-${HEPMC_VERSION} src

    # HEPMC HACK to support named weights
    cp ${data_dir}/WeightContainer.cc src/src/WeightContainer.cc
    cp ${data_dir}/WeightContainer.h src/HepMC/WeightContainer.h

    mkdir build
    cd build

    cmake \
        -DCMAKE_CXX_COMPILER=$(command -v g++) \
        -DCMAKE_BUILD_TYPE=Release \
        -Dbuild_docs:BOOL=OFF \
        -Dmomentum:STRING=GEV \
        -Dlength:STRING=MM \
        -DCMAKE_INSTALL_PREFIX=${ins_dir} \
        -S ../src

    # ./configure --prefix=${inst_dir} --with-momentum=GEV --with-length=MM

    make -j4
    make install

    rm_tmp_dir
}

function install_fastjet () {

    download $pkg_dir/fastjet-${FASTJET_VERSION}.tar.gz http://fastjet.fr/repo/fastjet-${FASTJET_VERSION}.tar.gz

    do_tmp_dir

    tar xvfz $pkg_dir/fastjet-${FASTJET_VERSION}.tar.gz

    cd fastjet-${FASTJET_VERSION}

    ./configure \
        --prefix=${ins_dir} \
        --enable-pyext=yes

    make -j4
    make check
    make install

    rm_tmp_dir
}

function install_lhapdf () {

    download $pkg_dir/LHAPDF-${LHAPDF_VERSION}.tar.gz https://lhapdf.hepforge.org/downloads/?f=LHAPDF-${LHAPDF_VERSION}.tar.gz

    do_tmp_dir

    tar xvz $pkg_dir/LHAPDF-${LHAPDF_VERSION}.tar.gz
    cd LHAPDF-${LHAPDF_VERSION}

    ./configure \
        --prefix=${ins_dir}

    make -j4
    make install

    rm_tmp_dir
}

function install_pythia () {

    download $pkg_dir/pythia${PYTHIA_VERSION}.tgz https://pythia.org/download/pythia${PYTHIA_VERSION:0:2}/pythia${PYTHIA_VERSION}.tgz

    do_tmp_dir

    tar xfz $pkg_dir/pythia${PYTHIA_VERSION}.tgz
    cd pythia${PYTHIA_VERSION}

    if [ -f $ins_dir/MG5_aMC/Template/NLO/MCatNLO/Scripts/JetMatching.h ] ; then
        echo " >> copy plugin needed for FxFx"
        cp $ins_dir/MG5_aMC/Template/NLO/MCatNLO/Scripts/JetMatching.h include/Pythia8Plugins/JetMatching.h
    fi

    ./configure \
        --prefix=${ins_dir} \
        --arch=Linux \
        --cxx=g++ \
        --with-gzip \
        --with-hepmc2=${ins_dir} \
        --with-hepmc2-include=${ins_dir}/include \
        --with-lhapdf6=${ins_dir} \
        --with-fastjet3=${ins_dir} \
        --cxx-common="-O2 -m64 -pedantic -W -Wall -Wshadow -fPIC -std=c++11 -DHEPMC2HACK" \
        --cxx-shared="-shared -std=c++11"

    make -j4
    make install

    cd $ins_dir/bin
    sed -e s/"if \[ \"\$VAR\" = \"LDFLAGS\" ]\; then OUT+=\" -ld\""/"if \[ \"\$VAR\" = \"LDFLAGS\" ]\; then OUT+=\" -ldl\""/g pythia8-config > pythia8-config.tmp
    mv pythia8-config.tmp pythia8-config
    chmod ug+x pythia8-config
    cd $tmp_dir

    rm_tmp_dir
}

function install_delphes () {

    download $pkg_dir/Delphes-${DELPHES_VERSION}.tar.gz http://cp3.irmp.ucl.ac.be/downloads/Delphes-${DELPHES_VERSION}.tar.gz

    tar -zxf $pkg_dir/Delphes-${DELPHES_VERSION}.tar.gz -C ${ins_dir}
    cd ${ins_dir}
    mv Delphes-${DELPHES_VERSION} Delphes
    cd Delphes
    make -j2
    cd ${top_dir}
}

function install_mg_py8_interface () {

    download $pkg_dir/MG5aMC_PY8_interface_V${MG5aMC_PY8_INTERFACE_VERSION}.tar.gz http://madgraph.phys.ucl.ac.be/Downloads/MG5aMC_PY8_interface/MG5aMC_PY8_interface_V${MG5aMC_PY8_INTERFACE_VERSION}.tar.gz

    do_tmp_dir

    mkdir -p $tmp_dir/MG5aMC_PY8_interface
    tar -xzvf $pkg_dir/MG5aMC_PY8_interface_V${MG5aMC_PY8_INTERFACE_VERSION}.tar.gz --directory=$tmp_dir/MG5aMC_PY8_interface
    cd $tmp_dir/MG5aMC_PY8_interface
    python3 compile.py ${ins_dir}/ --pythia8_makefile $(find ${ins_dir} -type d -name MG5_aMC)
    mkdir -p ${ins_dir}/MG5_aMC/HEPTools/MG5aMC_PY8_interface
    cp *.h ${ins_dir}/MG5_aMC/HEPTools/MG5aMC_PY8_interface/
    cp *_VERSION_ON_INSTALL ${ins_dir}/MG5_aMC/HEPTools/MG5aMC_PY8_interface/
    cp MG5aMC_PY8_interface ${ins_dir}/MG5_aMC/HEPTools/MG5aMC_PY8_interface/

    rm_tmp_dir
}

function config_mg () {

    # Change python version in mg bin
    sed -i 's|/usr/bin/env python3.*|/usr/bin/env python3.8|g' ${ins_dir}/MG5_aMC/bin/mg5_aMC

    # Change the MadGraph5_aMC@NLO configuration settings
    mg_config_file=${ins_dir}/MG5_aMC/input/mg5_configuration.txt
    cp ${ins_dir}/MG5_aMC/input/.mg5_configuration_default.txt ${mg_config_file}

    sed -i "s|# fastjet.*|fastjet = ${ins_dir}/bin/fastjet-config|g" ${mg_config_file}
    sed -i "s|# pythia8_path.*|pythia8_path = ${ins_dir}|g" ${mg_config_file}
    sed -i "/mg5amc_py8_interface_path =/s/^# //g" ${mg_config_file}
    sed -i "s|# eps_viewer.*|eps_viewer = "$(command -v ghostscript)"|g" ${mg_config_file}
    sed -i "s|# automatic_html_opening.*|automatic_html_opening = False|g" ${mg_config_file}
    sed -i "s|# run_mode = 2|run_mode = 0|g" ${mg_config_file}
    sed -i "s|# nb_core.*|nb_core = 1|g" ${mg_config_file}
    sed -i "s|# fortran_compiler.*|fortran_compiler = "$(command -v gfortran)"|g" ${mg_config_file}
    sed -i "s|# delphes_path.*|delphes_path = ../Delphes|g" ${mg_config_file}
    sed -i "s|# lhapdf_py2.*|lhapdf = ${ins_dir}/bin/lhapdf-config|g" ${mg_config_file}
    sed -i "s|# lhapdf_py3.*|lhapdf_py3 = ${ins_dir}/bin/lhapdf-config|g" ${mg_config_file}

}

function create_venv () {
    $PYTHON -m venv $ins_dir/venv
    source $ins_dir/venv/bin/activate
    pip install --upgrade pip gnureadline
    deactivate
}

function create_setup_file () {
    sed "s|__INS_DIR__|${ins_dir}|g" ${data_dir}/setup_mg_pythia_delphes.sh > ${ins_dir}/setup_mg_pythia_delphes.sh
}

function dowload_pdfs () {
    cd ${ins_dir}/share/LHAPDF
    # NNPDF23
    dowload_pdf NNPDF23_lo_as_0130_qed
    cd ${top_dir}
}

function copy_scripts () {
    cd ${top_dir}
    mkdir ${ins_dir}/scripts
    cp ${top_dir}/scripts/* ${ins_dir}/scripts/
}


# -------
# Install
# -------

mkdir -p ${ins_dir}
mkdir -p ${top_dir}/pkgs

# ROOT
install_setup_root

# MG
if [ ! -d ${ins_dir}/MG5_aMC ] ; then
    install_mg
fi

# HepMC
if [ ! -f $ins_dir/lib/libHepMC.so ] ; then
    install_hepmc
fi

# FastJet
if [ ! -f $ins_dir/lib/libfastjet.so ] ; then
    install_fastjet
fi

# LHAPDF
if [ ! -f $ins_dir/bin/lhapdf-config ] ; then
    install_lhapdf
fi

# Pythia8
if [ ! -f $ins_dir/bin/pythia8-config ] ; then
    install_pythia
fi

# Delphes
if [ ! -d $ins_dir/Delphes ] ; then
    install_delphes
fi

# Install MadGraph5_aMC@NLO for Python 3 and PYTHIA 8 interface
if [ ! -d $ins_dir/MG5_aMC/HEPTools/MG5aMC_PY8_interface ] ; then
    install_mg_py8_interface
fi

# Config, venv, setup, scripts, pdfs
config_mg
create_venv
create_setup_file
copy_scripts
dowload_pdfs

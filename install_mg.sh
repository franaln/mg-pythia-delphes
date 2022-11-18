top_dir=$PWD
tmp_dir=$PWD/tmp
ins_dir=$PWD/mg_pythia_delphes

CXX=$(command -v g++)
PYTHON=/usr/local/bin/python3.8
# PYTHON_CONFIG=$(find /usr/local/ -iname "python-config.py")
PYTHON_CONFIG=/usr/local/lib/python3.8/config-3.8-x86_64-linux-gnu/python-config.py


function install_root () {
    cd ${ins_dir}
    wget https://root.cern/download/root_v6.24.06.Linux-centos7-x86_64-gcc4.8.tar.gz
    tar xvfz root_v6.24.06.Linux-centos7-x86_64-gcc4.8.tar.gz
    rm -rf root_v6.24.06.Linux-centos7-x86_64-gcc4.8.tar.gz
}

function setup_root () {    
    source ${ins_dir}/root/bin/thisroot.sh
}

function do_tmp_dir () {
    mkdir -p ${tmp_dir}
    cd ${tmp_dir} 
}

function rm_tmp_dir () {
    cd $top_dir
    rm -rf ${tmp_dir}
}

# HepMC
function install_hepmc () {
  
    HEPMC_VERSION=2.06.09

    do_tmp_dir

    wget http://hepmc.web.cern.ch/hepmc/releases/hepmc${HEPMC_VERSION}.tgz 
    tar xvfz hepmc${HEPMC_VERSION}.tgz 
    mv hepmc${HEPMC_VERSION} src 

    # HEPMC HACK to support named weights
    cp ${top_dir}/WeightContainer.cc src/src/WeightContainer.cc
    cp ${top_dir}/WeightContainer.h src/HepMC/WeightContainer.h

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

# FastJet
function install_fastjet () {

    FASTJET_VERSION=3.3.4

    do_tmp_dir

    wget http://fastjet.fr/repo/fastjet-${FASTJET_VERSION}.tar.gz
    tar xvfz fastjet-${FASTJET_VERSION}.tar.gz
    cd fastjet-${FASTJET_VERSION}
    ./configure \
        --prefix=${ins_dir} \
        --enable-pyext=yes
    make -j4
    make check
    make install
    
    rm_tmp_dir
}

# LHAPDF
function install_lhapdf () {

    LHAPDF_VERSION=6.5.1

    do_tmp_dir

    wget https://lhapdf.hepforge.org/downloads/?f=LHAPDF-${LHAPDF_VERSION}.tar.gz -O LHAPDF-${LHAPDF_VERSION}.tar.gz
    tar xvfz LHAPDF-${LHAPDF_VERSION}.tar.gz
    cd LHAPDF-${LHAPDF_VERSION}
    ./configure \
        --prefix=${ins_dir}

    make -j4
    make install

    rm_tmp_dir
}

# PYTHIA
function install_pythia () {

    PYTHIA_VERSION=8306

    do_tmp_dir

    wget "https://pythia.org/download/pythia${PYTHIA_VERSION:0:2}/pythia${PYTHIA_VERSION}.tgz"
    tar xvfz pythia${PYTHIA_VERSION}.tgz
    cd pythia${PYTHIA_VERSION}
    ./configure --help
    ./configure \
        --prefix=${ins_dir} \
        --arch=Linux \
        --cxx=g++ \
        --with-gzip \
        --with-hepmc2=${ins_dir} \
        --with-hepmc2-include=${ins_dir}/include \
        --with-lhapdf6=${ins_dir} \
        --with-fastjet3=${ins_dir} \
        --cxx-common="-O2 -m64 -pedantic -W -Wall -Wshadow -fPIC -std=c++11" \
        --cxx-shared="-shared -std=c++11"
    make -j4
    make install

    rm_tmp_dir
}

# Delphes
function install_delphes () {

    DELPHES_VERSION=3.5.0
    
    cd ${ins_dir}

    wget --quiet http://cp3.irmp.ucl.ac.be/downloads/Delphes-${DELPHES_VERSION}.tar.gz
    tar -zxf Delphes-${DELPHES_VERSION}.tar.gz
    rm -rf Delphes-${DELPHES_VERSION}.tar.gz
    mv Delphes-${DELPHES_VERSION} Delphes
    cd Delphes
    make

    cd ${top_dir}

}

# Install MadGraph5_aMC@NLO for Python 3 and PYTHIA 8 interface
function install_mg () {

    MG_VERSION=3.4.1
    MG5aMC_PY8_INTERFACE_VERSION=1.3

    # MG
    cd ${ins_dir}
    wget https://launchpad.net/mg5amcnlo/3.0/3.4.x/+download/MG5_aMC_v${MG_VERSION}.tar.gz
    mkdir -p ${ins_dir}/MG5_aMC
    tar -xzvf MG5_aMC_v${MG_VERSION}.tar.gz --strip=1 --directory=MG5_aMC
    rm MG5_aMC_v${MG_VERSION}.tar.gz

    # MG5aMC_PY8_interface
    do_tmp_dir
    wget http://madgraph.phys.ucl.ac.be/Downloads/MG5aMC_PY8_interface/MG5aMC_PY8_interface_V${MG5aMC_PY8_INTERFACE_VERSION}.tar.gz
    mkdir -p MG5aMC_PY8_interface
    tar -xzvf MG5aMC_PY8_interface_V${MG5aMC_PY8_INTERFACE_VERSION}.tar.gz --directory=MG5aMC_PY8_interface
    cd MG5aMC_PY8_interface
    python3 compile.py ${ins_dir}/ --pythia8_makefile $(find ${ins_dir} -type d -name MG5_aMC)
    mkdir -p ${ins_dir}/MG5_aMC/HEPTools/MG5aMC_PY8_interface
    cp *.h ${ins_dir}/MG5_aMC/HEPTools/MG5aMC_PY8_interface/ 
    cp *_VERSION_ON_INSTALL ${ins_dir}/MG5_aMC/HEPTools/MG5aMC_PY8_interface/ 
    cp MG5aMC_PY8_interface ${ins_dir}/MG5_aMC/HEPTools/MG5aMC_PY8_interface/
    rm_tmp_dir
}

function config_mg () {

    # Change python version in mg bin
    sed -i 's|/usr/bin/env python3|/usr/bin/env python3.8|g' ${ins_dir}/MG5_aMC/bin/mg5_aMC    

    # Change the MadGraph5_aMC@NLO configuration settings
    mg_config_file=${ins_dir}/MG5_aMC/input/mg5_configuration.txt

    sed -i 's|# fastjet.*|fastjet = ${ins_dir}/bin/fastjet-config|g'   ${mg_config_file}
    sed -i 's|# pythia8_path.*|pythia8_path = ${ins_dir}|g' ${mg_config_file}
    sed -i '/mg5amc_py8_interface_path =/s/^# //g' ${mg_config_file}
    sed -i 's|# eps_viewer.*|eps_viewer = '$(command -v ghostscript)'|g' ${mg_config_file}
    sed -i 's|# automatic_html_opening.*|automatic_html_openening = False|g' ${mg_config_file}
    sed -i 's|# run_mode = 2|run_mode = 0|g' ${mg_config_file}
    sed -i 's|# nb_core.*|nb_core = 1|g' ${mg_config_file}
    sed -i 's|# fortran_compiler.*|fortran_compiler = '$(command -v gfortran)'|g' ${mg_config_file}
    sed -i 's|# delphes_path.*|delphes_path = ../Delphes|g' ${mg_config_file}
    
    echo "lhapdf = ${ins_dir}/bin/lhapdf-config"     >> ${mg_config_file}
    echo "lhapdf_py3 = ${ins_dir}/bin/lhapdf-config" >> ${mg_config_file}

    # FIX
    setup_file=${ins_dir}/setup_mg_pythia_delphes.sh
    
    echo ". ${ins_dir}/root/bin/thisroot.sh" >> ${setup_file}

    echo export PYTHONPATH=${ins_dir}/python:${ins_dir}/MG5_aMC:${ins_dir}/lib:${PYTHONPATH} >> ${setup_file}
    echo export LD_LIBRARY_PATH=${ins_dir}/lib:$LD_LIBRARY_PATH            >> ${setup_file}
    echo export PATH=${ins_dir}/Delphes:${ins_dir}/MG5_aMC/bin:$PATH       >> ${setup_file}
    echo export PYTHIA8DATA=${ins_dir}/share/Pythia8/xmldoc/               >> ${setup_file}

    mkdir ${ins_dir}/python
    cp six.py ${ins_dir}/python/
   
}

function dowload_pdf () {
    wget http://lhapdfsets.web.cern.ch/lhapdfsets/current/$1.tar.gz -O $1.tar.gz
    tar xvfz $1.tar.gz
    rm $1.tar.gz
}

function dowload_pdfs () {
    cd ${ins_dir}/share/LHAPDF
    # NNPDF23
    dowload_pdf NNPDF23_lo_as_0130_qed
    cd ${top_dir}
}

mkdir -p ${ins_dir}

install_root
setup_root
install_hepmc
install_fastjet
install_lhapdf
install_pythia
install_delphes
install_mg
config_mg
dowload_pdfs

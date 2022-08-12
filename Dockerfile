# Dockerfile for MadGraph + Pythia8 + Delphes
# Based on https://github.com/scailfin/MadGraph5_aMC-NLO

ARG BUILDER_IMAGE=centos:centos8
FROM ${BUILDER_IMAGE} as builder

USER root
WORKDIR /

SHELL [ "/bin/bash", "-c" ]

# Fix yum repos
RUN sed -i 's/mirrorlist/#mirrorlist/g' /etc/yum.repos.d/CentOS-* && \
    sed -i 's|#baseurl=http://mirror.centos.org|baseurl=http://vault.centos.org|g' /etc/yum.repos.d/CentOS-*

# CMake provided by base image
RUN yum update -y && \
    yum install -y \
      gcc \
      gcc-c++ \
      gcc-gfortran \
      cmake \
      vim \
      zlib \
      zlib-devel \
      bzip2 \
      bzip2-devel \
      rsync \
      wget \
      ghostscript \
      bc \
      cmake \
      python39 \
      python39-six \
      python39-devel \
      git && \
    yum clean all


# Install directory
RUN mkdir /usr/local/venv

# Install ROOT
RUN cd /usr/local/venv && \
    wget https://root.cern/download/root_v6.26.06.Linux-centos8-x86_64-gcc8.5.tar.gz && \
    tar xvfz root_v6.26.06.Linux-centos8-x86_64-gcc8.5.tar.gz && \
    rm root_v6.26.06.Linux-centos8-x86_64-gcc8.5.tar.gz && \
    rm -rf /usr/local/venv/root/tutorials && \
    source root/bin/thisroot.sh


# Install HepMC
ARG HEPMC_VERSION=2.06.11
RUN mkdir /code && \
    cd /code && \
    wget http://hepmc.web.cern.ch/hepmc/releases/hepmc${HEPMC_VERSION}.tgz && \
    tar xvfz hepmc${HEPMC_VERSION}.tgz && \
    mv HepMC-${HEPMC_VERSION} src && \
    cmake \
      -DCMAKE_CXX_COMPILER=$(command -v g++) \
      -DCMAKE_BUILD_TYPE=Release \
      -Dbuild_docs:BOOL=OFF \
      -Dmomentum:STRING=MEV \
      -Dlength:STRING=MM \
      -DCMAKE_INSTALL_PREFIX=/usr/local/venv \
      -S src \
      -B build && \
    cmake build -L && \
    cmake --build build -- -j$(($(nproc) - 1)) && \
    cmake --build build --target install && \
    rm -rf /code

# Install FastJet
ARG FASTJET_VERSION=3.3.4
RUN mkdir /code && \
    cd /code && \
    wget http://fastjet.fr/repo/fastjet-${FASTJET_VERSION}.tar.gz && \
    tar xvfz fastjet-${FASTJET_VERSION}.tar.gz && \
    cd fastjet-${FASTJET_VERSION} && \
    ./configure --help && \
    export CXX=$(command -v g++) && \
    export PYTHON=$(command -v python3) && \
    export PYTHON_CONFIG=$(find /usr/local/ -iname "python-config.py") && \
    ./configure \
      --prefix=/usr/local/venv \
      --enable-pyext=yes && \
    make -j$(($(nproc) - 1)) && \
    make check && \
    make install && \
    rm -rf /code

# Install LHAPDF
ARG LHAPDF_VERSION=6.5.1
RUN mkdir /code && \
    cd /code && \
    wget https://lhapdf.hepforge.org/downloads/?f=LHAPDF-${LHAPDF_VERSION}.tar.gz -O LHAPDF-${LHAPDF_VERSION}.tar.gz && \
    tar xvfz LHAPDF-${LHAPDF_VERSION}.tar.gz && \
    cd LHAPDF-${LHAPDF_VERSION} && \
    ./configure --help && \
    export CXX=$(command -v g++) && \
    export PYTHON=$(command -v python3) && \
    ./configure \
      --prefix=/usr/local/venv && \
    make -j$(($(nproc) - 1)) && \
    make install && \
    rm -rf /code

# Install PYTHIA
ARG PYTHIA_VERSION=8306
RUN mkdir /code && \
    cd /code && \
    wget "https://pythia.org/download/pythia${PYTHIA_VERSION:0:2}/pythia${PYTHIA_VERSION}.tgz" && \
    tar xvfz pythia${PYTHIA_VERSION}.tgz && \
    cd pythia${PYTHIA_VERSION} && \
    ./configure --help && \
    export PYTHON_MINOR_VERSION=${PYTHON_VERSION::3} && \
    ./configure \
      --prefix=/usr/local/venv \
      --arch=Linux \
      --cxx=g++ \
      --enable-64bit \
      --with-gzip \
      --with-hepmc2=/usr/local/venv \
      --with-lhapdf6=/usr/local/venv \
      --with-fastjet3=/usr/local/venv \
      --with-python-bin=/usr/local/venv/bin/ \
      --with-python-lib=/usr/local/venv/lib/python${PYTHON_MINOR_VERSION} \
      --with-python-include=/usr/local/include/python${PYTHON_MINOR_VERSION} \
      --cxx-common="-O2 -m64 -pedantic -W -Wall -Wshadow -fPIC -std=c++11" \
      --cxx-shared="-shared -std=c++11" && \
    make -j$(($(nproc) - 1)) && \
    make install && \
    rm -rf /code

# Install BOOST (needed? it doesn't seem so)
# c.f. https://www.boost.org/doc/libs/1_76_0/more/getting_started/unix-variants.html
# ARG BOOST_VERSION=1.76.0
# hadolint ignore=SC2046

#RUN mkdir -p /code && \
#    cd /code && \
#    BOOST_VERSION_UNDERSCORE="${BOOST_VERSION//\./_}" && \
#    curl --silent --location --remote-name "https://boostorg.jfrog.io/artifactory/main/release/${BOOST_VERSION}/source/boost_${BOOST_VERSION_UNDERSCORE}.tar.gz" && \
#    tar -xzf "boost_${BOOST_VERSION_UNDERSCORE}.tar.gz" && \
#    cd "boost_${BOOST_VERSION_UNDERSCORE}" && \
#    source scl_source enable devtoolset-8 && \
#    ./bootstrap.sh --help && \
#    ./bootstrap.sh \
#      --prefix=/usr/local/venv \
#      --with-python=$(command -v python3) && \
#    ./b2 install -j$(($(nproc) - 1)) && \
#    cd / && \
#    rm -rf /code

# Install Delphes
ARG DELPHES_VERSION=3.5.0

RUN cd /usr/local/venv && \
    wget --quiet http://cp3.irmp.ucl.ac.be/downloads/Delphes-${DELPHES_VERSION}.tar.gz && \  
    tar -zxf Delphes-${DELPHES_VERSION}.tar.gz && \
    rm -rf Delphes-${DELPHES_VERSION}.tar.gz && \
    mv Delphes-${DELPHES_VERSION} Delphes && \
    cd Delphes && \
    source /usr/local/venv/root/bin/thisroot.sh && \
    make


# Install MadGraph5_aMC@NLO for Python 3 and PYTHIA 8 interface
ARG MG_VERSION=3.4.0
ARG MG5aMC_PY8_INTERFACE_VERSION=1.2

RUN cd /usr/local/venv && \
    wget --quiet https://launchpad.net/mg5amcnlo/3.0/3.4.x/+download/MG5_aMC_v${MG_VERSION}.tar.gz && \
    mkdir -p /usr/local/venv/MG5_aMC && \
    tar -xzvf MG5_aMC_v${MG_VERSION}.tar.gz --strip=1 --directory=MG5_aMC && \
    rm MG5_aMC_v${MG_VERSION}.tar.gz && \
    echo "Installing MG5aMC_PY8_interface" && \
    mkdir /code && \
    cd /code && \
    wget --quiet http://madgraph.phys.ucl.ac.be/Downloads/MG5aMC_PY8_interface/MG5aMC_PY8_interface_V${MG5aMC_PY8_INTERFACE_VERSION}.tar.gz && \
    mkdir -p /code/MG5aMC_PY8_interface && \
    tar -xzvf MG5aMC_PY8_interface_V${MG5aMC_PY8_INTERFACE_VERSION}.tar.gz --directory=MG5aMC_PY8_interface && \
    cd MG5aMC_PY8_interface && \
    python3 compile.py /usr/local/venv/ --pythia8_makefile $(find /usr/local/ -type d -name MG5_aMC) && \
    mkdir -p /usr/local/venv/MG5_aMC/HEPTools/MG5aMC_PY8_interface && \
    cp *.h /usr/local/venv/MG5_aMC/HEPTools/MG5aMC_PY8_interface/ && \
    cp *_VERSION_ON_INSTALL /usr/local/venv/MG5_aMC/HEPTools/MG5aMC_PY8_interface/ && \
    cp MG5aMC_PY8_interface /usr/local/venv/MG5_aMC/HEPTools/MG5aMC_PY8_interface/ && \
    rm -rf /code && \
    rm -rf /usr/local/venv/MG5_aMC/tests/

# Change the MadGraph5_aMC@NLO configuration settings
ARG MG_CONFIG_FILE=/usr/local/venv/MG5_aMC/input/mg5_configuration.txt

RUN sed -i '/fastjet =/s/^# //g' ${MG_CONFIG_FILE} && \
    sed -i '/lhapdf_py3 =/s/^# //g' ${MG_CONFIG_FILE} && \
    sed -i 's|# pythia8_path.*|pythia8_path = /usr/local/venv|g' ${MG_CONFIG_FILE} && \
    sed -i '/mg5amc_py8_interface_path =/s/^# //g' ${MG_CONFIG_FILE} && \
    sed -i 's|# eps_viewer.*|eps_viewer = '$(command -v ghostscript)'|g' ${MG_CONFIG_FILE} && \
    sed -i 's|# fortran_compiler.*|fortran_compiler = '$(command -v gfortran)'|g' ${MG_CONFIG_FILE} && \
    sed -i 's|# delphes_path.*|delphes_path = /usr/local/venv/Delphes|g' ${MG_CONFIG_FILE} && \
    echo "lhapdf = /usr/local/venv/bin/lhapdf-config"     >> ${MG_CONFIG_FILE} && \
    echo "lhapdf_py3 = /usr/local/venv/bin/lhapdf-config" >> ${MG_CONFIG_FILE} && \
    echo "fastjet = /usr/local/venv/bin/fastjet-config"   >> ${MG_CONFIG_FILE}


# Create non-root user "docker"
RUN useradd --shell /bin/bash -m docker && \
   cp /root/.bashrc /home/docker/ && \
   mkdir /home/docker/data && \
   chown -R --from=root docker /home/docker && \
   chown -R --from=root docker /usr/local/venv && \
   chown -R --from=503 docker /usr/local/venv/MG5_aMC

# Use en_US.utf8 locale to avoid issues with ASCII encoding
# as C.UTF-8 not available on CentOS 7
ENV LC_ALL=en_US.utf8
ENV LANG=en_US.utf8

ENV HOME /home/docker
WORKDIR ${HOME}/data

ENV PYTHONPATH=/usr/local/venv/MG5_aMC:/usr/local/venv/lib:${PYTHONPATH}
ENV LD_LIBRARY_PATH=/usr/local/venv/lib:$LD_LIBRARY_PATH
ENV PATH=${HOME}/.local/bin:/root/.local/bin:$PATH
ENV PATH=/usr/local/venv/Delphes:/usr/local/venv/MG5_aMC/bin:$PATH

# TODO: Install NLO dependencies independently for greater control
# Running the NLO process forces install of cuttools and iregi
# RUN if [ -f /root/.profile ];then cp /root/.profile ${HOME}/.profile;fi && \
#     cp /root/.bashrc ${HOME}/.bashrc && \
#     printf "\nsource scl_source enable devtoolset-8\n" >> ${HOME}/.bash_profile && \
#     python3 -m pip --no-cache-dir install --upgrade pip setuptools wheel && \
#     python3 -m pip --no-cache-dir install six numpy && \
#     sed -i 's|# f2py_compiler_py3.*|f2py_compiler_py3 = '$(command -v f2py)'|g' /usr/local/venv/MG5_aMC/input/mg5_configuration.txt && \
#     echo "exit" | mg5_aMC && \
#     echo "install ninja" | mg5_aMC && \
#     echo "install collier" | mg5_aMC && \
#     echo "generate p p > e+ e- aEW=2 aS=0 [QCD]; output test_nlo" | mg5_aMC && \
#     rm -rf test_nlo && \
#     rm -rf $(find /usr/local/ -type d -name HEPToolsInstallers) && \
#     rm py.py

RUN if [ -f /root/.profile ];then cp /root/.profile ${HOME}/.profile;fi && \
    cp /root/.bashrc ${HOME}/.bashrc && \
    printf "\nsource scl_source enable devtoolset-8\n" >> ${HOME}/.bash_profile && \
    python3 -m pip --no-cache-dir install --upgrade pip setuptools wheel && \
    python3 -m pip --no-cache-dir install six numpy && \
    sed -i 's|# f2py_compiler_py3.*|f2py_compiler_py3 = '$(command -v f2py)'|g' /usr/local/venv/MG5_aMC/input/mg5_configuration.txt && \
    echo "exit" | mg5_aMC && \
    echo "install ninja" | mg5_aMC && \
    echo "install collier" | mg5_aMC && \
    rm -rf $(find /usr/local/ -type d -name HEPToolsInstallers) && \
    rm py.py

# Download PDFs
RUN cd /usr/local/venv/share/LHAPDF && \
    wget http://lhapdfsets.web.cern.ch/lhapdfsets/current/NNPDF23_lo_as_0130_qed.tar.gz -O NNPDF23_lo_as_0130_qed.tar.gz && \
    tar xvfz  NNPDF23_lo_as_0130_qed.tar.gz && \
    rm NNPDF23_lo_as_0130_qed.tar.gz


# Default user is root to avoid uid write permission problems with volumes
ENV HOME /root
WORKDIR ${HOME}/data

ENTRYPOINT ["/bin/bash", "-l", "-c"]
CMD ["/bin/bash"]


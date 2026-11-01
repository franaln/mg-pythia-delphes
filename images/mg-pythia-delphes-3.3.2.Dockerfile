# Checked recipe: original scientific versions retained; requires a reviewed source lock.
# Dockerfile for MadGraph + Pythia8 + Delphes
# using the following versions:
# python=3.8
# ROOT=6.24.02
# MadGraph=3.3.2
# hepmc=2.06.09
# fastjet= 3.3.4
# LHAPDF=6.3.0
# pythia=8.306
# Delphes=3.5.0
# MG5aMC_PY8_interface=1.3

FROM ubuntu:20.04
ARG DEBIAN_FRONTEND=noninteractive
ARG BUILD_JOBS=4
ARG SOURCE_LOCK=images/sources.lock.json
LABEL org.opencontainers.image.title="mg-pythia-delphes" \
      org.opencontainers.image.version="3.3.2"

USER root
WORKDIR /

SHELL [ "/bin/bash", "-e", "-o", "pipefail", "-c" ]

RUN test "$(uname -m)" = x86_64 || { echo 'This recipe requires linux/amd64 (x86_64 ROOT binaries)' >&2; exit 1; }
RUN [[ "${BUILD_JOBS}" =~ ^[1-9][0-9]*$ ]] || { echo 'BUILD_JOBS must be a positive integer' >&2; exit 1; }

#
RUN apt-get -qq -y update && \
    apt-get -y install \
      gcc \
      g++ \
      gfortran \
      patch \
      cmake \
      zlib1g-dev \
      rsync \
      ca-certificates \
      wget \
      ghostscript \
      bc \
      python3 \
      python3-pip \
      python3-dev \
      python3-venv \
      coreutils \
      git && \
    apt-get -y autoclean && \
    apt-get -y autoremove && \
    rm -rf /var/lib/apt/lists/*

# All external source archives must match the reviewed lock before extraction.
COPY images/sources.py /opt/mgpd-build/sources.py
COPY images/fetch-source /usr/local/bin/fetch-source
COPY ${SOURCE_LOCK} /opt/mgpd-build/sources.lock.json
RUN chmod 0755 /usr/local/bin/fetch-source

# Install directory
ARG INSTALL_DIR=/mg_pythia_delphes
ARG TMP_DIR=/code
ARG DATA_TMP_DIR=/data

# Create install directories
RUN mkdir -p ${INSTALL_DIR} && \
    mkdir -p ${INSTALL_DIR}/python && \
    mkdir -p ${INSTALL_DIR}/scripts

# Create tmp build directories
RUN mkdir -p ${DATA_TMP_DIR}

# Install ROOT
ARG ROOT_VERSION=root_v6.24.02.Linux-ubuntu20-x86_64-gcc9.3.tar.gz
ARG ROOT_URL=https://root.cern/download/${ROOT_VERSION}
RUN fetch-source "${ROOT_URL}" "${ROOT_VERSION}" && \
    tar xvfz ${ROOT_VERSION} -C ${INSTALL_DIR} && \
    rm -rf ${INSTALL_DIR}/root/tutorials && \
    rm ${ROOT_VERSION}

# Install MG
ARG MG_VERSION=3.3.2
ARG MG_URL=https://launchpad.net/mg5amcnlo/3.0/3.3.x/+download/MG5_aMC_v${MG_VERSION}.tar.gz
COPY data/no_update_3_3_2.patch ${DATA_TMP_DIR}
COPY data/six.py ${INSTALL_DIR}/python
RUN fetch-source "${MG_URL}" "MG5_aMC_v${MG_VERSION}.tar.gz" && \
    mkdir ${INSTALL_DIR}/MG5_aMC && \
    tar -xzvf MG5_aMC_v${MG_VERSION}.tar.gz --strip=1 --directory=${INSTALL_DIR}/MG5_aMC && \
    rm MG5_aMC_v${MG_VERSION}.tar.gz && \
    # Patch to not try to update MG
    patch ${INSTALL_DIR}/MG5_aMC/madgraph/interface/madgraph_interface.py < ${DATA_TMP_DIR}/no_update_3_3_2.patch


# Install HepMC
ARG HEPMC_VERSION=02_06_09
COPY data/WeightContainer* ${DATA_TMP_DIR}
RUN mkdir ${TMP_DIR} && \
    cd ${TMP_DIR} && \
    fetch-source "https://gitlab.cern.ch/hepmc/HepMC/-/archive/HEPMC_${HEPMC_VERSION}/HepMC-HEPMC_${HEPMC_VERSION}.tar.gz" "HepMC-HEPMC_${HEPMC_VERSION}.tar.gz" && \
    tar -xvzf HepMC-HEPMC_${HEPMC_VERSION}.tar.gz && \
    mv HepMC-HEPMC_${HEPMC_VERSION} src && \
    # HEPMC HACK to support named weights
    cp ${DATA_TMP_DIR}/WeightContainer.cc src/src/WeightContainer.cc && \
    cp ${DATA_TMP_DIR}/WeightContainer.h  src/HepMC/WeightContainer.h && \
    mkdir build && \
    cd build && \
    cmake \
    -DCMAKE_CXX_COMPILER=$(command -v g++) \
    -DCMAKE_BUILD_TYPE=Release \
    -Dbuild_docs:BOOL=OFF \
    -Dmomentum:STRING=MEV \
    -Dlength:STRING=MM \
    -DCMAKE_INSTALL_PREFIX=${INSTALL_DIR} \
    -S ../src && \
    make -j"${BUILD_JOBS}" && \
    make install && \
    rm -rf ${TMP_DIR}

# Install FastJet
ARG FASTJET_VERSION=3.3.4
RUN mkdir ${TMP_DIR} && \
    cd ${TMP_DIR} && \
    fetch-source "https://fastjet.fr/repo/fastjet-${FASTJET_VERSION}.tar.gz" "fastjet-${FASTJET_VERSION}.tar.gz" && \
    tar xvfz fastjet-${FASTJET_VERSION}.tar.gz && \
    cd fastjet-${FASTJET_VERSION} && \
    ./configure \
    --prefix=${INSTALL_DIR} \
    --enable-pyext=yes && \
    make -j"${BUILD_JOBS}" && \
    make check && \
    make install && \
    rm -rf ${TMP_DIR}

RUN echo "CXX=$(command -v g++)" > /setup_build.sh && \
    echo "export PYTHON=/usr/bin/python3.8" >> /setup_build.sh && \
    echo "export PYTHON_CONFIG=/usr/lib/python3.8/config-3.8-x86_64-linux-gnu/python-config.py" >> /setup_build.sh && \
    echo "export PYTHON_INCLUDE=-I/usr/include/python3.8" >> /setup_build.sh && \
    echo "source ${INSTALL_DIR}/root/bin/thisroot.sh" >> /setup_build.sh

# Install LHAPDF
ARG LHAPDF_VERSION=6.3.0
RUN mkdir ${TMP_DIR} && \
    cd ${TMP_DIR} && \
    fetch-source "https://lhapdf.hepforge.org/downloads/?f=LHAPDF-${LHAPDF_VERSION}.tar.gz" "LHAPDF-${LHAPDF_VERSION}.tar.gz" && \
    tar xvfz LHAPDF-${LHAPDF_VERSION}.tar.gz && \
    cd LHAPDF-${LHAPDF_VERSION} && \
    source /setup_build.sh && \
    ./configure \
      --prefix=${INSTALL_DIR} && \
    make -j"${BUILD_JOBS}" && \
    make install && \
    rm -rf ${TMP_DIR}

# Install PYTHIA
ARG PYTHIA_VERSION=8306
RUN mkdir ${TMP_DIR} && \
    cd ${TMP_DIR} && \
    fetch-source "https://pythia.org/download/pythia${PYTHIA_VERSION:0:2}/pythia${PYTHIA_VERSION}.tgz" "pythia${PYTHIA_VERSION}.tgz" && \
    tar xvfz pythia${PYTHIA_VERSION}.tgz && \
    cd pythia${PYTHIA_VERSION} && \
    cp ${INSTALL_DIR}/MG5_aMC/Template/NLO/MCatNLO/Scripts/JetMatching.h include/Pythia8Plugins/JetMatching.h && \
    source /setup_build.sh && \
    ./configure \
      --prefix=${INSTALL_DIR} \
      --arch=Linux \
      --cxx=g++ \
      --with-gzip \
      --with-hepmc2=${INSTALL_DIR} \
      --with-hepmc2-include=${INSTALL_DIR}/include \
      --with-lhapdf6=${INSTALL_DIR} \
      --with-fastjet3=${INSTALL_DIR} \
      --cxx-common="-O2 -pedantic -W -Wall -Wshadow -fPIC -std=c++11 -DHEPMC2HACK" \
      --cxx-shared="-shared -std=c++11" && \
    make -j"${BUILD_JOBS}" && \
    make install && \
    cd ${INSTALL_DIR}/bin && \
    sed -e s/"if \[ \"\$VAR\" = \"LDFLAGS\" ]\; then OUT+=\" -ld\""/"if \[ \"\$VAR\" = \"LDFLAGS\" ]\; then OUT+=\" -ldl\""/g pythia8-config > pythia8-config.tmp && \
    mv pythia8-config.tmp pythia8-config && \
    chmod ug+x pythia8-config && \
    rm -rf ${TMP_DIR}

# Install Delphes
ARG DELPHES_VERSION=3.5.0
RUN fetch-source "https://cp3.irmp.ucl.ac.be/downloads/Delphes-${DELPHES_VERSION}.tar.gz" "Delphes-${DELPHES_VERSION}.tar.gz" && \
    tar -zxf Delphes-${DELPHES_VERSION}.tar.gz -C ${INSTALL_DIR} && \
    rm -rf Delphes-${DELPHES_VERSION}.tar.gz && \
    cd ${INSTALL_DIR} && \
    mv Delphes-${DELPHES_VERSION} Delphes && \
    cd Delphes && \
    source /setup_build.sh && \
    make -j"${BUILD_JOBS}"

# Install MG-Pythia8 interface
ARG MG5aMC_PY8_INTERFACE_VERSION=1.3
RUN fetch-source "https://madgraph.phys.ucl.ac.be/Downloads/MG5aMC_PY8_interface/MG5aMC_PY8_interface_V${MG5aMC_PY8_INTERFACE_VERSION}.tar.gz" "MG5aMC_PY8_interface_V${MG5aMC_PY8_INTERFACE_VERSION}.tar.gz" && \
    mkdir ${TMP_DIR} && \
    cd ${TMP_DIR} && \
    mkdir -p $TMP_DIR/MG5aMC_PY8_interface && \
    tar -xzvf /MG5aMC_PY8_interface_V${MG5aMC_PY8_INTERFACE_VERSION}.tar.gz --directory=${TMP_DIR}/MG5aMC_PY8_interface && \
    cd ${TMP_DIR}/MG5aMC_PY8_interface && \
    python3 compile.py ${INSTALL_DIR}/ --pythia8_makefile $(find ${INSTALL_DIR} -type d -name MG5_aMC) && \
    mkdir -p ${INSTALL_DIR}/MG5_aMC/HEPTools/MG5aMC_PY8_interface && \
    cp *.h ${INSTALL_DIR}/MG5_aMC/HEPTools/MG5aMC_PY8_interface/ && \
    cp *_VERSION_ON_INSTALL ${INSTALL_DIR}/MG5_aMC/HEPTools/MG5aMC_PY8_interface/ && \
    cp MG5aMC_PY8_interface ${INSTALL_DIR}/MG5_aMC/HEPTools/MG5aMC_PY8_interface/ && \
    rm -rf /MG5aMC_PY8_interface_V${MG5aMC_PY8_INTERFACE_VERSION}.tar.gz && \
    rm -rf ${TMP_DIR}

# Change the MadGraph5_aMC@NLO configuration settings
ARG MG_CONFIG_FILE=${INSTALL_DIR}/MG5_aMC/input/mg5_configuration.txt
RUN cp ${INSTALL_DIR}/MG5_aMC/input/.mg5_configuration_default.txt ${MG_CONFIG_FILE} && \
    sed -i "s|# fastjet.*|fastjet = ${INSTALL_DIR}/bin/fastjet-config|g" ${MG_CONFIG_FILE} && \
    sed -i "s|# pythia8_path.*|pythia8_path = ${INSTALL_DIR}|g" ${MG_CONFIG_FILE} && \
    sed -i "/mg5amc_py8_interface_path =/s/^# //g" ${MG_CONFIG_FILE} && \
    sed -i "s|# eps_viewer.*|eps_viewer = "$(command -v ghostscript)"|g" ${MG_CONFIG_FILE} && \
    sed -i "s|# automatic_html_opening.*|automatic_html_opening = False|g" ${MG_CONFIG_FILE} && \
    sed -i "s|# run_mode = 2|run_mode = 0|g" ${MG_CONFIG_FILE} && \
    sed -i "s|# nb_core.*|nb_core = 1|g" ${MG_CONFIG_FILE} && \
    sed -i "s|# fortran_compiler.*|fortran_compiler = "$(command -v gfortran)"|g" ${MG_CONFIG_FILE} && \
    sed -i "s|# delphes_path.*|delphes_path = ../Delphes|g" ${MG_CONFIG_FILE} && \
    sed -i "s|# lhapdf_py2.*|lhapdf = ${INSTALL_DIR}/bin/lhapdf-config|g" ${MG_CONFIG_FILE} && \
    sed -i "s|# lhapdf_py3.*|lhapdf_py3 = ${INSTALL_DIR}/bin/lhapdf-config|g" ${MG_CONFIG_FILE} && \
    sed -i "s|# auto_update = 7|auto_update = 0|g" ${MG_CONFIG_FILE}

# Fix this o_O
RUN cp ${INSTALL_DIR}/MG5_aMC/Template/LO/Source/.make_opts ${INSTALL_DIR}/MG5_aMC/Template/LO/Source/make_opts

# Create venv
RUN python3 -m venv ${INSTALL_DIR}/venv && \
    source ${INSTALL_DIR}/venv/bin/activate && \
    pip install --upgrade pip gnureadline

# Create setup file
COPY images/setup_mg_pythia_delphes.sh ${DATA_TMP_DIR}
RUN sed "s|__INS_DIR__|${INSTALL_DIR}|g" ${DATA_TMP_DIR}/setup_mg_pythia_delphes.sh > /setup_mg_pythia_delphes.sh

# Install/compile needed tools for loops
RUN source /setup_mg_pythia_delphes.sh && \
    echo "install ninja" | mg5_aMC && \
    echo "install collier" | mg5_aMC

# build cuttools
RUN cd ${INSTALL_DIR}/MG5_aMC/vendor/CutTools && \
    make clean && \
    make -j"${BUILD_JOBS}"

# build iregi
RUN cd ${INSTALL_DIR}/MG5_aMC/vendor/IREGI/src && \
    make clean && \
    make -j"${BUILD_JOBS}"

# build StdHEP
RUN cd ${INSTALL_DIR}/MG5_aMC/vendor/StdHEP && \
    make -j"${BUILD_JOBS}"

# Install loop_qcd_qed_sm model
COPY data/loop_qcd_qed_sm.tar.gz /
RUN tar -xvzf loop_qcd_qed_sm.tar.gz -C ${INSTALL_DIR}/MG5_aMC/models && \
    rm /loop_qcd_qed_sm.tar.gz

# Install SM_LQHH_MU2-LAM3-LAM1-LAM2
COPY data/SM_LQHH_MU2-LAM3-LAM1-LAM2.tar.gz /
RUN tar -xvzf SM_LQHH_MU2-LAM3-LAM1-LAM2.tar.gz -C ${INSTALL_DIR}/MG5_aMC/models && \
    rm /SM_LQHH_MU2-LAM3-LAM1-LAM2.tar.gz

# Download PDFs
# 230000: NNPDF23_nlo_as_0119
# 247000: NNPDF23_lo_as_0130_qed
# 260000: NNPDF30_nlo_as_0118
# 303000: NNPDF30_nlo_as_0118_hessian
# 91500: PDF4LHC15_nnlo_mc
COPY images/download_pdf.sh ${INSTALL_DIR}/scripts/
RUN chmod 0755 ${INSTALL_DIR}/scripts/download_pdf.sh
RUN sed -i  "s|__INS_DIR__|${INSTALL_DIR}|g" ${INSTALL_DIR}/scripts/download_pdf.sh && \
    bash ${INSTALL_DIR}/scripts/download_pdf.sh NNPDF23_nlo_as_0119         && \
    bash ${INSTALL_DIR}/scripts/download_pdf.sh NNPDF23_lo_as_0130_qed      && \
    bash ${INSTALL_DIR}/scripts/download_pdf.sh NNPDF30_nlo_as_0118         && \
    bash ${INSTALL_DIR}/scripts/download_pdf.sh NNPDF30_nlo_as_0118_hessian && \
    bash ${INSTALL_DIR}/scripts/download_pdf.sh PDF4LHC15_nnlo_mc

# Fail if an expected MadGraph configuration substitution or installation is missing.
RUN python3 -c 'from pathlib import Path; import os; root=os.environ["INSTALL_DIR"]; p=Path(os.environ["MG_CONFIG_FILE"]); lines=p.read_text().splitlines(); settings={k.strip():v.strip() for line in lines if line.strip() and not line.lstrip().startswith("#") and "=" in line for k,v in [line.split("=",1)]}; expected={"fastjet":root+"/bin/fastjet-config","pythia8_path":root,"delphes_path":"../Delphes","automatic_html_opening":"False","run_mode":"0","nb_core":"1","auto_update":"0","lhapdf_py3":root+"/bin/lhapdf-config"}; assert all(settings.get(k)==v for k,v in expected.items()), settings' && \
    test -x ${INSTALL_DIR}/MG5_aMC/bin/mg5_aMC && \
    test -x ${INSTALL_DIR}/MG5_aMC/HEPTools/MG5aMC_PY8_interface/MG5aMC_PY8_interface && \
    test -x ${INSTALL_DIR}/Delphes/DelphesHepMC2 && \
    test -x ${INSTALL_DIR}/Delphes/root2lhco && \
    test -x ${INSTALL_DIR}/Delphes/lhco2root && \
    test -d ${INSTALL_DIR}/share/Pythia8/xmldoc

# Preserve an inventory and the exact download lock in the image.
RUN mkdir -p ${INSTALL_DIR}/share/mgpd-image && \
    cp /opt/mgpd-build/sources.lock.json ${INSTALL_DIR}/share/mgpd-image/sources.lock.json && \
    printf '%s\n' "MadGraph=${MG_VERSION}" "ROOT=${ROOT_VERSION}" "HepMC=${HEPMC_VERSION}" "FastJet=${FASTJET_VERSION}" "LHAPDF=${LHAPDF_VERSION}" "Pythia=${PYTHIA_VERSION}" "Delphes=${DELPHES_VERSION}" "MG5aMC_PY8_interface=${MG5aMC_PY8_INTERFACE_VERSION}" > ${INSTALL_DIR}/share/mgpd-image/versions.txt

# Create non-root user "docker"
RUN useradd --shell /bin/bash -m docker && \
   cp /root/.bashrc /home/docker/ && \
   mkdir /home/docker/work && \
   chown -R docker:docker /home/docker && \
   chown -R docker:docker ${INSTALL_DIR} && \
   chmod -R a+rX ${INSTALL_DIR}


ARG bashrc=/etc/profile
RUN echo source /setup_mg_pythia_delphes.sh >> ${bashrc}

RUN rm -rf ${DATA_TMP_DIR} /setup_build.sh

ENV HOME=/home/docker
USER docker
WORKDIR ${HOME}/work

RUN source /setup_mg_pythia_delphes.sh && \
    python -c 'import ROOT; print(ROOT.gROOT.GetVersion())' && \
    command -v mg5_aMC && command -v hadd && command -v root2lhco && command -v lhco2root && \
    fastjet-config --version && lhapdf-config --version

ENTRYPOINT ["/bin/bash", "-l", "-c"]
CMD ["/bin/bash"]

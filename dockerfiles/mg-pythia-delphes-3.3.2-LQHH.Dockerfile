FROM franaln/mg-pythia-delphes:3.3.2

USER root
WORKDIR /

SHELL [ "/bin/bash", "-c" ]

# Install directory
ARG INSTALL_DIR=/mg_pythia_delphes

# Install loop_qcd_qed_sm model
COPY data/loop_qcd_qed_sm.tar.gz /
RUN tar -xvzf loop_qcd_qed_sm.tar.gz -C ${INSTALL_DIR}/MG5_aMC/models && \
    rm /loop_qcd_qed_sm.tar.gz

# Install SM_LQHH_MU2-LAM3-LAM1-LAM2
COPY data/SM_LQHH_MU2-LAM3-LAM1-LAM2.tar.gz /
RUN tar -xvzf SM_LQHH_MU2-LAM3-LAM1-LAM2.tar.gz -C ${INSTALL_DIR}/MG5_aMC/models && \
    rm /SM_LQHH_MU2-LAM3-LAM1-LAM2.tar.gz

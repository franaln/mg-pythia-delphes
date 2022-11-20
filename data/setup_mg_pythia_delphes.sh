MG_NAME="mg_pythia_delphes"
MG_DIR="__INS_DIR__"

deactivate () {
    # reset old environment variables
    if [ -n "${_OLD_VIRTUAL_PATH:-}" ] ; then
        PATH="${_OLD_VIRTUAL_PATH:-}"
        export PATH
        unset _OLD_VIRTUAL_PATH
    fi
    if [ -n "${_OLD_VIRTUAL_PYTHONHOME:-}" ] ; then
        PYTHONHOME="${_OLD_VIRTUAL_PYTHONHOME:-}"
        export PYTHONHOME
        unset _OLD_VIRTUAL_PYTHONHOME
    fi

    if [ -n "${_OLD_VIRTUAL_LIB:-}" ] ; then
        LD_LIBRARY_PATH="${_OLD_VIRTUAL_LIB:-}"
        export LD_LIBRARY_PATH
        unset _OLD_VIRTUAL_LIB
    fi

    if [ -n "${_OLD_VIRTUAL_PYTHONPATH:-}" ] ; then
        PYTHONPATH="${_OLD_VIRTUAL_PYTHONPATH:-}"
        export PYTHONPATH
        unset _OLD_VIRTUAL_PYTHONPATH
    fi

    unset PYTHIA8DATA

    # This should detect bash and zsh, which have a hash command that must
    # be called to get it to forget past commands.  Without forgetting
    # past commands the $PATH changes we made may not be respected
    if [ -n "${BASH:-}" -o -n "${ZSH_VERSION:-}" ] ; then
        hash -r
    fi

    if [ -n "${_OLD_VIRTUAL_PS1:-}" ] ; then
        PS1="${_OLD_VIRTUAL_PS1:-}"
        export PS1
        unset _OLD_VIRTUAL_PS1
    fi

    unset VIRTUAL_ENV
    if [ ! "${1:-}" = "nondestructive" ] ; then
    # Self destruct!
        unset -f deactivate
    fi
}

# unset irrelevant variables
deactivate nondestructive

VIRTUAL_ENV="__INS_DIR__/venv"
export VIRTUAL_ENV

# PATH
_OLD_VIRTUAL_PATH="$PATH"
export PATH="$VIRTUAL_ENV/bin:$MG_DIR/Delphes:$MG_DIR/MG5_aMC/bin:$MG_DIR/root/bin:$MG_DIR/scripts:$PATH"

# unset PYTHONHOME if set
# this will fail if PYTHONHOME is set to the empty string (which is bad anyway)
# could use `if (set -u; : $PYTHONHOME) ;` in bash
if [ -n "${PYTHONHOME:-}" ] ; then
    _OLD_VIRTUAL_PYTHONHOME="${PYTHONHOME:-}"
    unset PYTHONHOME
fi

# ROOT
. /mnt/R5/hep_tools/mg_pythia_delphes/root/bin/thisroot.sh

# PYTHONPATH
_OLD_VIRTUAL_PYTHONPATH="$PYTHONPATH"
export PYTHONPATH="$MG_DIR/python:$MG_DIR/MG5_aMC:$MG_DIR/lib:$MG_DIR/root/lib:$PYTHONPATH"

# LIBS
_OLD_VIRTUAL_LIB="$LD_LIBRARY_PATH"
export LD_LIBRARY_PATH="$MG_DIR/lib:$MG_DIR/root/lib:$LD_LIBRARY_PATH"

# PYTHIA DATA
export PYTHIA8DATA="$MG_DIR/share/Pythia8/xmldoc/"


if [ -z "${VIRTUAL_ENV_DISABLE_PROMPT:-}" ] ; then
    _OLD_VIRTUAL_PS1="${PS1:-}"
    if [ "x(venv) " != x ] ; then
	PS1="($MG_NAME) ${PS1:-}"
    else
    PS1="($MG_NAME)$PS1"
    fi
    export PS1
fi

# This should detect bash and zsh, which have a hash command that must
# be called to get it to forget past commands.  Without forgetting
# past commands the $PATH changes we made may not be respected
if [ -n "${BASH:-}" -o -n "${ZSH_VERSION:-}" ] ; then
    hash -r
fi



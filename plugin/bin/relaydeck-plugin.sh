#!/bin/sh
# RelayDeck Herdr plugin entry. Every manifest command starts here, so a Mac without Python or the
# developer tools never reaches /usr/bin/python3 (whose stub would start their installer).
# Python is used only after a RelayDeck installation has been found and validated by absolute path.
#
# usage: relaydeck-plugin.sh {build | setup | pair | status | open <setup|pair|status>}
set -eu

# Nothing is looked up through PATH here: every tool below is an absolute path or a shell builtin.
# The caller's PATH is kept only to hand to the validated launcher, which validates what it finds.
caller_path=${PATH-}
PATH=
export PATH

EXIT_USAGE=2
EXIT_NOT_READY=3
REPO_URL="https://github.com/Hybridhash/relaydeck"
TAG="v0.2.2"

FIND=/usr/bin/find
READLINK=/usr/bin/readlink

# True when $1 is owned by the current user or by root.
owner_ok() {
    [ -O "$1" ] && return 0
    [ -n "$("$FIND" "$1" -maxdepth 0 -user 0 -print 2>/dev/null)" ]
}

# True when group or others can write $1.
loose_mode() {
    [ -n "$("$FIND" "$1" -maxdepth 0 \( -perm -020 -o -perm -002 \) -print 2>/dev/null)" ]
}

sticky() {
    [ -n "$("$FIND" "$1" -maxdepth 0 -perm -1000 -print 2>/dev/null)" ]
}

# Every directory from the root down to the parent of $1 must be owned by this user or root and
# not writable by group or others (a sticky directory is accepted). Links are followed one
# component at a time by the system, so each listed directory is what is walked.
ancestry_ok() {
    _p=$1
    while :; do
        case $_p in
            */*) _p=${_p%/*} ;;
            *) break ;;
        esac
        _d=$_p
        [ -n "$_d" ] || _d=/
        [ -d "$_d" ] || return 1
        owner_ok "$_d" || return 1
        if loose_mode "$_d" && ! sticky "$_d"; then
            return 1
        fi
        [ "$_d" = / ] && break
    done
    return 0
}

# Print the fully resolved absolute path of $1 when it is a safe executable file; fail otherwise.
# Links must be owned by this user or root, the original and the resolved ancestry must both be
# trusted, and the target must be a regular file owned by this user that others cannot write.
resolve_trusted() {
    _path=$1
    case $_path in
        /*) ;;
        *) return 1 ;;
    esac
    ancestry_ok "$_path" || return 1
    _hops=0
    while [ -L "$_path" ]; do
        owner_ok "$_path" || return 1
        _hops=$((_hops + 1))
        [ "$_hops" -le 8 ] || return 1
        _target=$("$READLINK" "$_path") || return 1
        case $_target in
            /*) _path=$_target ;;
            *) _path=${_path%/*}/$_target ;;
        esac
        ancestry_ok "$_path" || return 1
    done
    # Collapse "." and ".." through the physical directory, then re-check the resolved ancestry.
    _dir=${_path%/*}
    _base=${_path##*/}
    _dir=$(cd -P -- "${_dir:-/}" 2>/dev/null && pwd -P) || return 1
    _path=${_dir%/}/$_base
    ancestry_ok "$_path" || return 1
    [ -f "$_path" ] && [ ! -L "$_path" ] || return 1
    [ -O "$_path" ] || return 1
    loose_mode "$_path" && return 1
    [ -x "$_path" ] || return 1
    printf '%s\n' "$_path"
}

install_steps() {
    printf '%s\n' \
        "RelayDeck is not installed for this user." \
        "Install it once from a terminal, then run this again:" \
        "  1. Install uv: https://docs.astral.sh/uv/" \
        "  2. uv tool install --managed-python --python 3.13 \"relaydeck[companion] @ git+$REPO_URL@$TAG\"" \
        "  3. relaydeck setup" \
        "Nothing was installed by this plugin."
}

# The validated tool interpreter, or nothing. Fixed candidates under the user's home only.
find_python() {
    _home=${HOME:-}
    case $_home in
        /*) ;;
        *) return 1 ;;
    esac
    resolve_trusted "$_home/.local/share/uv/tools/relaydeck/bin/python"
}

launcher_dir() {
    case $0 in
        */*) _l=${0%/*} ;;
        *) _l=. ;;
    esac
    cd -P -- "$_l" && pwd -P
}

# Herdr's own executable from HERDR_BIN_PATH, validated like the RelayDeck tool.
herdr_binary() {
    _h=${HERDR_BIN_PATH:-}
    [ -n "$_h" ] || return 1
    resolve_trusted "$_h"
}

# Never forwarded: the plugin does not read or pass Herdr's context.
unset HERDR_PLUGIN_CONTEXT_JSON

mode=${1:-}
case $mode in
    build | setup | pair | status)
        [ "$#" -eq 1 ] || exit "$EXIT_USAGE"
        ;;
    open)
        [ "$#" -eq 2 ] || exit "$EXIT_USAGE"
        case $2 in
            setup | pair | status) ;;
            *) exit "$EXIT_USAGE" ;;
        esac
        ;;
    *)
        printf '%s\n' "usage: relaydeck-plugin.sh {build|setup|pair|status|open <setup|pair|status>}" >&2
        exit "$EXIT_USAGE"
        ;;
esac

here=$(launcher_dir) || exit "$EXIT_NOT_READY"
python=$(find_python) || python=

if [ -n "$python" ] && [ -f "$here/relaydeck-plugin" ]; then
    PATH=$caller_path
    export PATH
    exec "$python" -I "$here/relaydeck-plugin" "$@"
fi

# RelayDeck is not installed (or not safely): nothing is installed or run from here.
case $mode in
    build)
        printf '%s\n' "RelayDeck is not installed yet; open RelayDeck: Set up from Herdr for the steps."
        exit 0
        ;;
    open)
        # Actions have no terminal: only the setup popup is opened, whichever action was chosen.
        if herdr=$(herdr_binary); then
            exec "$herdr" plugin pane open --plugin relaydeck --entrypoint setup
        fi
        printf '%s\n' "HERDR_BIN_PATH is not usable. Open RelayDeck from Herdr, or run it from a terminal." \
            "Nothing was run."
        exit "$EXIT_NOT_READY"
        ;;
    *)
        install_steps
        printf '%s' "Press Enter to close. "
        read -r _ || true
        exit "$EXIT_NOT_READY"
        ;;
esac

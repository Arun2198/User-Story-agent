#!/usr/bin/env bash
# Save your NVIDIA API key for this machine and load it into this terminal.
# Run it with:  source scripts/setup_keys.sh
# The key goes into a private file in your home folder (not in the repository).

_nv_file="${NVIDIA_ENV_FILE:-$HOME/.nvidia_env}"
_nv_rc="${NVIDIA_RC_FILE:-$HOME/.bashrc}"

_nv_ask() {
    local prompt="$1" value
    printf '%s' "$prompt" >&2
    IFS= read -rs value
    printf '\n' >&2
    printf '%s' "$value"
}

_nv_ok() {
    case "$1" in
        *[[:space:]\'\"\\]*) return 1 ;;
    esac
    [ ${#1} -ge 20 ]
}

_nv_key="$(_nv_ask 'NVIDIA_API_KEY (paste it; nothing shows on screen): ')"
if ! _nv_ok "$_nv_key"; then
    echo "That does not look like a key (too short, or it has spaces or quotes). Nothing was saved." >&2
    unset _nv_key _nv_file _nv_rc
    return 1 2>/dev/null || exit 1
fi
case "$_nv_key" in
    nvapi-*) ;;
    *) echo "Note: NVIDIA keys normally start with nvapi-. Saving it anyway." >&2 ;;
esac

(
    umask 077
    printf "export NVIDIA_API_KEY='%s'\n" "$_nv_key" > "$_nv_file"
)
chmod 600 "$_nv_file"
if ! grep -qs "nvidia_env" "$_nv_rc" 2>/dev/null; then
    printf '[ -f "%s" ] && . "%s"\n' "$_nv_file" "$_nv_file" >> "$_nv_rc"
fi

# shellcheck disable=SC1090
. "$_nv_file"
unset _nv_key
echo "Saved to $_nv_file (readable only by you)."
echo "NVIDIA_API_KEY: ${NVIDIA_API_KEY:+set}"
unset _nv_file _nv_rc

#!/usr/bin/env bash
# Save your NVIDIA API keys for this machine and load them into this terminal.
# Run it with:  source scripts/setup_keys.sh
# The keys go into a private file in your home folder (not in the repository).

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

_nv_main="$(_nv_ask 'NVIDIA_API_KEY (paste it; nothing shows on screen): ')"
if ! _nv_ok "$_nv_main"; then
    echo "That does not look like a key (too short, or it has spaces or quotes). Nothing was saved." >&2
    unset _nv_main _nv_file _nv_rc
    return 1 2>/dev/null || exit 1
fi
case "$_nv_main" in
    nvapi-*) ;;
    *) echo "Note: NVIDIA keys normally start with nvapi-. Saving it anyway." >&2 ;;
esac

_nv_judge="$(_nv_ask 'NVIDIA_API_KEY_JUDGE (paste a second key, or press Enter to reuse the first): ')"
if [ -n "$_nv_judge" ] && ! _nv_ok "$_nv_judge"; then
    echo "The judge key does not look right. Nothing was saved." >&2
    unset _nv_main _nv_judge _nv_file _nv_rc
    return 1 2>/dev/null || exit 1
fi

(
    umask 077
    {
        printf "export NVIDIA_API_KEY='%s'\n" "$_nv_main"
        if [ -n "$_nv_judge" ]; then
            printf "export NVIDIA_API_KEY_JUDGE='%s'\n" "$_nv_judge"
        fi
    } > "$_nv_file"
)
chmod 600 "$_nv_file"
if ! grep -qs "nvidia_env" "$_nv_rc" 2>/dev/null; then
    printf '[ -f "%s" ] && . "%s"\n' "$_nv_file" "$_nv_file" >> "$_nv_rc"
fi

# shellcheck disable=SC1090
. "$_nv_file"
unset _nv_main _nv_judge
echo "Saved to $_nv_file (readable only by you)."
echo "NVIDIA_API_KEY:       ${NVIDIA_API_KEY:+set}"
if [ -n "${NVIDIA_API_KEY_JUDGE:-}" ]; then
    echo "NVIDIA_API_KEY_JUDGE: set (its own key)"
else
    echo "NVIDIA_API_KEY_JUDGE: not set, the judge will use NVIDIA_API_KEY"
fi
unset _nv_file _nv_rc

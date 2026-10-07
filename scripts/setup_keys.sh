#!/usr/bin/env bash
# Save your API keys for this machine and load them into this terminal.
# Run it with:  source scripts/setup_keys.sh
# You can give one key or both. Press Enter to skip a provider.
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

_nv_anthropic="$(_nv_ask 'ANTHROPIC_API_KEY (paste it, or press Enter to skip): ')"
_nv_nvidia="$(_nv_ask 'NVIDIA_API_KEY (paste it, or press Enter to skip): ')"

if [ -z "$_nv_anthropic" ] && [ -z "$_nv_nvidia" ]; then
    echo "No key given. Nothing was saved." >&2
    unset _nv_anthropic _nv_nvidia _nv_file _nv_rc
    return 1 2>/dev/null || exit 1
fi
for _nv_k in "$_nv_anthropic" "$_nv_nvidia"; do
    if [ -n "$_nv_k" ] && ! _nv_ok "$_nv_k"; then
        echo "A key does not look right (too short, or it has spaces or quotes). Nothing was saved." >&2
        unset _nv_anthropic _nv_nvidia _nv_k _nv_file _nv_rc
        return 1 2>/dev/null || exit 1
    fi
done
unset _nv_k

(
    umask 077
    {
        if [ -n "$_nv_anthropic" ]; then
            printf "export ANTHROPIC_API_KEY='%s'\n" "$_nv_anthropic"
        fi
        if [ -n "$_nv_nvidia" ]; then
            printf "export NVIDIA_API_KEY='%s'\n" "$_nv_nvidia"
        fi
    } > "$_nv_file"
)
chmod 600 "$_nv_file"
if ! grep -qs "nvidia_env" "$_nv_rc" 2>/dev/null; then
    printf '[ -f "%s" ] && . "%s"\n' "$_nv_file" "$_nv_file" >> "$_nv_rc"
fi

# Forget keys from earlier in this terminal, then load the saved file.
unset ANTHROPIC_API_KEY NVIDIA_API_KEY
# shellcheck disable=SC1090
. "$_nv_file"
unset _nv_anthropic _nv_nvidia
echo "Saved to $_nv_file (readable only by you)."
for _nv_name in ANTHROPIC_API_KEY NVIDIA_API_KEY; do
    if [ -n "${!_nv_name:-}" ]; then
        echo "$_nv_name: set"
    else
        echo "$_nv_name: not set"
    fi
done
unset _nv_name
unset _nv_file _nv_rc

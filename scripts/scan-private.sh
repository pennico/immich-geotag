#!/usr/bin/env bash
# Fails if a file or a commit contains a private pattern: home-network addresses, key-shaped strings, or anything in
# the extra list (CI: the repository secret PRIVATE_PATTERNS; locally: one regex per line in private/patterns.txt,
# which git ignores). Prints file:line, commit ids and counts only, never the matching text, a matching file name or
# any part of the list.
#
# usage: scripts/scan-private.sh                    tracked and untracked (not ignored) files of this checkout
#        scripts/scan-private.sh <dir>              every file under <dir>, e.g. an unpacked `git archive`
#        scripts/scan-private.sh --history <rev> [<tag>]
#                                                   every commit in <rev> (git rev-list syntax: A..B, or B for all of
#                                                   it) after the baseline in scripts/scan-private.baseline: author,
#                                                   committer, message, added lines and file names; plus the annotated
#                                                   <tag> object if given
#        scripts/scan-private.sh --fingerprint      length and short hash of the extra list (local use: compare with
#                                                   the length CI prints)
#        scripts/scan-private.sh --length           length of the extra list only (what CI prints)
#        scripts/scan-private.sh --self-test        a planted hit must fail, a malformed or empty pattern must error
#
# env:   PRIVATE_PATTERNS              the extra list: '|'-joined, or one pattern per line like the file ('#' comments
#                                      and blank lines dropped); wins over private/patterns.txt
#        REQUIRE_PRIVATE_PATTERNS=1    no extra list is an error (CI: pushes to main, tags, same-repo pull requests)
#
# Binary files are scanned as text (grep -a). Commits reachable from the baseline were published before this scanner
# and are not re-scanned (the known old hits are recorded elsewhere, not rewritten).
#
# exit:  0 clean, 1 private patterns found, 2 setup error (list required but missing, malformed or empty-matching
#        pattern, missing folder, git or grep failure)
set -euo pipefail
cd "$(dirname "$0")/.."
SELF=scripts/scan-private.sh
# Written so that this file does not match itself ([-], no literal address): the scanner scans its own file too.
BUILTIN='192\.168\.[0-9]|(^|[^0-9])10\.[0-9]+\.[0-9]+\.[0-9]+|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]+\.[0-9]+|AIza[A-Za-z0-9_-]{10}|gh[pousr]_[A-Za-z0-9]{20}|sk-[A-Za-z0-9-]{20}|-----BEGIN [A-Z ]*PRIVATE KEY|pennico[-]ops'

die() { echo "scan-private: $*" >&2; exit 2; }
notice() { if [ -n "${GITHUB_ACTIONS:-}" ]; then echo "::notice::$*"; else echo "$*"; fi; }

extra_list() {  # the extra list as one '|'-joined line ('' when there is none); the secret and the file alike
  if [ -n "${PRIVATE_PATTERNS:-}" ]; then
    printf '%s\n' "$PRIVATE_PATTERNS"
  elif [ -f private/patterns.txt ]; then
    cat private/patterns.txt
  fi | tr -d '\r' | { grep -v '^[[:space:]]*\(#\|$\)' || true; } | paste -sd'|' -
}

# --- self-test (built-in patterns only; never touches the secret) ----------------------------------------------------
if [ "${1:-}" = --self-test ]; then
  t=$(mktemp -d); trap 'rm -rf "$t"' EXIT
  mkdir "$t/clean" "$t/hit" "$t/bin"
  echo 'nothing to see here' > "$t/clean/a.txt"
  printf 'one\nserver 192.168.%s here\n' '1.20' > "$t/hit/b.txt"
  printf '\0\1 192.168.%s \0' '7.7' > "$t/bin/c.bin"
  run() { set +e; env -u PRIVATE_PATTERNS REQUIRE_PRIVATE_PATTERNS=0 "$@" >/dev/null 2>&1; echo $?; set -e; }
  ok=1
  [ "$(run "$SELF" "$t/clean")" = 0 ] || { echo "self-test: a clean folder did not pass" >&2; ok=0; }
  [ "$(run "$SELF" "$t/hit")" = 1 ] || { echo "self-test: a planted hit was not found" >&2; ok=0; }
  [ "$(run "$SELF" "$t/bin")" = 1 ] || { echo "self-test: a hit in a binary file was not found" >&2; ok=0; }
  [ "$(run "$SELF" "$t/missing")" = 2 ] || { echo "self-test: a missing folder did not error" >&2; ok=0; }
  [ "$(run env PRIVATE_PATTERNS='(' "$SELF" "$t/clean")" = 2 ] || { echo "self-test: a malformed pattern did not error" >&2; ok=0; }
  [ "$(run env PRIVATE_PATTERNS='abc|' "$SELF" "$t/clean")" = 2 ] || { echo "self-test: an empty alternative did not error" >&2; ok=0; }
  [ "$(run env PRIVATE_PATTERNS='zz-not-there' "$SELF" "$t/clean")" = 0 ] || { echo "self-test: a valid extra list failed" >&2; ok=0; }
  [ "$(run env PRIVATE_PATTERNS="$(printf 'zz-not-there\nnothing')" "$SELF" "$t/clean")" = 1 ] \
    || { echo "self-test: a list with one pattern per line was not split" >&2; ok=0; }
  [ "$(run env REQUIRE_PRIVATE_PATTERNS=1 "$SELF" "$t/clean")" = 2 ] || [ -f private/patterns.txt ] \
    || { echo "self-test: a missing required list did not error" >&2; ok=0; }
  [ "$ok" = 1 ] || exit 1
  echo "self-test passed"; exit 0
fi

EXTRA=$(extra_list)

# --- fingerprint / length ----------------------------------------------------------------------------------------------
if [ "${1:-}" = --fingerprint ] || [ "${1:-}" = --length ]; then
  if [ -z "$EXTRA" ]; then echo "extra list: none"; exit 0; fi
  if [ "$1" = --length ]; then echo "extra list: ${#EXTRA} characters"; exit 0; fi
  if command -v sha256sum >/dev/null; then h=$(printf '%s' "$EXTRA" | sha256sum); else h=$(printf '%s' "$EXTRA" | shasum -a 256); fi
  echo "extra list: ${#EXTRA} characters, sha256 ${h:0:12}"; exit 0
fi

# --- the pattern: required, valid, and never matching empty text -----------------------------------------------------
if [ -z "$EXTRA" ]; then
  [ "${REQUIRE_PRIVATE_PATTERNS:-0}" = 1 ] && die "no extra list (secret PRIVATE_PATTERNS or private/patterns.txt) and one is required"
  notice "scan-private: built-in patterns only (no PRIVATE_PATTERNS or private/patterns.txt)"
  PATTERNS=$BUILTIN
else
  set +e; printf '' | grep -E -e "$EXTRA" >/dev/null 2>&1; rc=$?; set -e
  [ "$rc" -le 1 ] || die "the extra list is not a valid extended regex (not printed)"
  if printf '\n' | grep -qE -e "$EXTRA" 2>/dev/null; then die "the extra list matches empty text (an empty alternative?)"; fi
  PATTERNS="${BUILTIN}|${EXTRA}"
fi

# scan <base dir> <newline list of files>: prints file:line of each hit; exit 2 on a grep error
scan() {
  local base=$1 files=$2 hits
  [ -n "$files" ] || return 0
  # grep exits 1 for "no match" (fine) and 2 for an error: turn 2 into 255 so xargs stops and fails.
  if ! hits=$(cd "$base" && printf '%s\n' "$files" | tr '\n' '\0' \
      | xargs -0 sh -c 'grep -HnaiE -o -e "$0" -- "$@"; r=$?; [ "$r" -le 1 ] || exit 255' "$PATTERNS" \
      | cut -d: -f1,2 | sort -u); then
    die "grep failed while scanning"
  fi
  printf '%s' "$hits"
}
# names <newline list>: the positions (#N) of names that match, never the names
names() { printf '%s\n' "$1" | { grep -n -iE -e "$PATTERNS" || true; } | cut -d: -f1 | sed 's/^/#/' | paste -sd' ' -; }

# --- history mode ----------------------------------------------------------------------------------------------------
if [ "${1:-}" = --history ]; then
  range=${2:?usage: --history <rev> [<tag>]}; tag=${3:-}
  t=$(mktemp -d); trap 'rm -rf "$t"' EXIT
  not=()
  if [ -f scripts/scan-private.baseline ]; then
    b=$(grep -m1 -E '^[0-9a-f]{40}$' scripts/scan-private.baseline) || die "scripts/scan-private.baseline holds no commit id"
    git cat-file -e "$b^{commit}" 2>/dev/null || die "the baseline commit is not in this clone (fetch the full history)"
    not=(--not "$b")
  fi
  # shellcheck disable=SC2086  # the range may be several rev-list words
  commits=$(git rev-list $range ${not[@]+"${not[@]}"}) || die "git rev-list failed for the range"
  # Identities: every author, committer and tagger must be a GitHub noreply address whose name is its login (or
  # GitHub's own web-flow identity). Anything else (a machine's user@host, a real address) is reported, not printed.
  bad_ids=""
  check_id() {  # where, name, email
    local login
    case "$3" in
      noreply@github.com) [ "$2" = GitHub ] && return 0 ;;
      *@users.noreply.github.com)
        login=${3%@users.noreply.github.com}; login=${login#*+}
        [[ "$login" =~ ^[A-Za-z0-9-]+(\[bot\])?$ ]] && [ "$2" = "$login" ] && return 0 ;;
    esac
    bad_ids="${bad_ids}$1"$'\n'
  }
  n=0
  for c in $commits; do
    n=$((n + 1))
    git log -1 --format='%an%n%ae%n%cn%n%ce' "$c" > "$t/ids" || die "git log failed"
    { IFS= read -r an; IFS= read -r ae; IFS= read -r cn; IFS= read -r ce; } < "$t/ids"
    check_id "$c author" "$an" "$ae"; check_id "$c committer" "$cn" "$ce"
    git log -1 --format='%B' "$c" > "$t/$c.message" || die "git log failed"
    # every added line, as text, without renames or textconv (a rename or a binary file still shows its lines)
    git show --format= --no-color --unified=0 --no-renames --text --no-textconv --no-ext-diff "$c" > "$t/diff" \
      || die "git show failed"
    { grep -a '^+' "$t/diff" || true; } > "$t/$c.added-lines"
    git diff-tree -r -m --root --no-commit-id --name-only --no-renames "$c" > "$t/$c.file-names" \
      || die "git diff-tree failed"
  done
  rm -f "$t/ids" "$t/diff"
  if [ -n "$tag" ] && [ "$(git cat-file -t "$tag" 2>/dev/null)" = tag ]; then
    obj=$(git cat-file tag "$tag") || die "git cat-file failed"
    tl=$(printf '%s\n' "$obj" | sed -n 's/^tagger //p' | head -1)
    tn=${tl%% <*}; te=${tl#*<}; te=${te%%>*}
    check_id "tag $tag tagger" "$tn" "$te"
    printf '%s\n' "$obj" | sed '1,/^$/d' > "$t/tag-message"   # the message, after the header
  fi
  files=$(cd "$t" && find . -type f | sed 's|^\./||')
  hits=$(scan "$t" "$files")
  if [ -n "$hits$bad_ids" ]; then
    if [ -n "$bad_ids" ]; then
      echo "Identities that are not a GitHub noreply login (commit, role):" >&2
      printf '%s' "$bad_ids" >&2
    fi
    if [ -n "$hits" ]; then
      echo "Private patterns found in commits (commit.part:line):" >&2
      echo "$hits" >&2
    fi
    exit 1
  fi
  echo "No private patterns in $n commit(s)${tag:+ and tag $tag}."
  exit 0
fi

# --- tree mode -------------------------------------------------------------------------------------------------------
if [ $# -gt 0 ]; then
  [ -d "$1" ] || die "no such folder: $1"
  BASE=$(cd "$1" && pwd) || die "cannot enter $1"
  FILES=$(cd "$BASE" && find . -type f ! -path './.git/*') || die "find failed"
  FILES=$(printf '%s\n' "$FILES" | sed 's|^\./||')
else
  BASE=.
  FILES=$(git ls-files --cached --others --exclude-standard) || die "git ls-files failed"
fi
[ -z "$FILES" ] && { echo "no files to scan"; exit 0; }
HITS=$(scan "$BASE" "$FILES")
NAMES=$(names "$FILES")   # file names count too; their position in the list is printed, not the name
if [ -n "$HITS$NAMES" ]; then
  echo "Private patterns found (file:line):" >&2
  [ -n "$HITS" ] && echo "$HITS" >&2
  [ -n "$NAMES" ] && echo "In file names (position in the scanned list, run the script locally to see them): $NAMES" >&2
  exit 1
fi
echo "No private patterns in $(printf '%s\n' "$FILES" | wc -l | tr -d ' ') files."

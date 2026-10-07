#!/bin/sh
# PreToolUse guard (easy-setup, play-client "Agents can't link or write the project's client and runtime folders").
# Claude Code passes the tool call as JSON on stdin; exit 2 with a reason on stderr blocks it (exit 0 allows it).
# Blocks: a shell command or file edit that writes into this repo's client/ (the pristine game client), and a shell
# command that symlinks this repo's client/, env/, data/ or server/ into another folder (the 2026-10-07 makeshift
# copy: `for d in ... client data env server ...; do ln -s $R/$d $d; done`, after which a build wrote client/Wow.exe).
# Allows reads (ls, cat, shasum, ...), git, and the project's own commands (harness/hs, ./play, ./bootstrap), which
# guard client/ themselves (OS write protection + writer guards). Portable: no personal paths; the repo is this
# script's grandparent folder (or $CLAUDE_PROJECT_DIR).
here=$(cd "$(dirname "$0")" && pwd)
repo=${CLAUDE_PROJECT_DIR:-$(cd "$here/../.." && pwd)}
command -v python3 >/dev/null 2>&1 || exit 0
HS_GUARD_INPUT=$(cat)
export HS_GUARD_INPUT
exec python3 -I - "$repo" <<'PY'
import json, os, re, shlex, sys

repo = os.path.realpath(sys.argv[1])
CLIENT = os.path.join(repo, "client")
LINKED = ("client", "env", "data", "server")
READ_ONLY = {"ls", "cat", "less", "more", "head", "tail", "shasum", "sha256sum", "md5", "md5sum", "file", "stat",
             "du", "wc", "grep", "rg", "ugrep", "diff", "cmp", "strings", "otool", "codesign", "xxd", "hexdump",
             "od", "echo", "printf", "test", "[", "true", "false", "pwd", "realpath", "readlink", "basename", "dirname",
             "which", "type", "sort", "uniq", "cut", "awk", "jq", "tree", "mdls", "lsof", "pgrep", "ps", "open"}
TRUSTED = ("harness/hs", "./play", "./bootstrap", "play", "bootstrap")
WRITE_ALL_ARGS = {"rm", "rmdir", "touch", "mkdir", "chmod", "chflags", "chown", "chgrp", "truncate", "xattr",
                  "unlink", "shred", "setfile", "mv", "ln", "patch", "srm"}
WRITE_LAST_ARG = {"cp", "rsync", "ditto", "install", "scp", "gcp"}
INPLACE = {"sed", "perl", "ruby", "gsed"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
FIND_ACTIONS = ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls")
def block(why):
    sys.stderr.write("Blocked by .claude/hooks/guard-client.sh: " + why + "\n"
                     "client/ is the pristine game client and is never written by hand; env/, data/, server/ and "
                     "client/ must not be linked into other folders (2026-10-07: a makeshift copy with linked "
                     "folders overwrote client/Wow.exe). For an A/B comparison, check out the other commit in this "
                     "checkout (stack down, nothing else running) or use the harness. Reads, git and harness/hs "
                     "commands are fine.\n")
    sys.exit(2)


def in_client(path, cwd):
    """Is `path` (as written; may hold $VARS) this repo's client/ or inside it? Variables are unknown here: a path
    that starts with a variable and continues with /client is treated as client/."""
    p = path.strip()
    if not p:
        return False
    if re.match(r"^(\$\{?\w+\}?|~)/+client(/|$)", p, re.I):
        return True
    p = os.path.expanduser(p)
    if "$" in p:
        return False
    full = os.path.normpath(os.path.join(cwd, p))
    real = os.path.realpath(full)
    for cand in (full, real):
        c, r = os.path.normcase(cand).casefold(), os.path.normcase(CLIENT).casefold()
        if c == r or c.startswith(r + os.sep):
            return True
    return False


def check_file_tool(tool, inp, cwd):
    path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
    if path and in_client(path, cwd):
        block(f"{tool} would write {path}, inside this repo's client/.")


def segments(cmd):
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|<>()")
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        toks = list(lex)
    except ValueError:
        toks = cmd.split()
    seg, out = [], []
    for t in toks:
        if t and set(t) <= set(";&|()") :
            if seg:
                out.append(seg)
            seg = []
        else:
            seg.append(t)
    if seg:
        out.append(seg)
    return out


KEYWORDS = {"do", "then", "else", "elif", "{", "}", "!", "time", "nohup", "sudo", "command", "exec", "env", "xargs",
            "builtin", "noglob", "caffeinate", "timeout", "gtimeout"}


def verb_and_args(seg):
    i = 0
    while i < len(seg):
        t = seg[i]
        if re.match(r"^\w+=", t) or t in KEYWORDS or (seg[i - 1] in ("timeout", "gtimeout") if i else False) and re.match(r"^[\d.]+[smh]?$", t):
            i += 1
            continue
        if t.startswith("-") and i and seg[i - 1] in ("env", "sudo", "xargs", "nohup", "caffeinate", "timeout"):
            i += 1
            continue
        break
    return (seg[i] if i < len(seg) else ""), seg[i + 1:]


SHELL_OPT_ARG = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}   # shell options that take the next word


def inner_shell_code(base, args):
    """The command string a wrapped shell runs: `sh|bash|zsh ... -c '<code>'` (also -lc, -ec, `-o pipefail -c`, ...),
    `bash <<< '<code>'` (a here-string) or `eval <words>`. "" + STDIN when the shell reads its code from stdin (a pipe
    `... | sh`, a heredoc, `< file`), which the caller judges by the whole command. None when there's none (a plain
    `bash script.sh` runs a file the hook can't see, like any other program)."""
    if base == "eval":
        return " ".join(args)
    if base in SHELLS:
        k = 0
        while k < len(args):
            a = args[k]
            if a == "<<<":
                return args[k + 1] if k + 1 < len(args) else ""
            if a in SHELL_OPT_ARG or (re.match(r"^[-+][a-zA-Z]*[oO]$", a) and "c" not in a[1:]):
                k += 2          # -o pipefail, +O extglob, -eo pipefail: the option's value isn't a script
                continue
            if a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
                return args[k + 1] if k + 1 < len(args) else ""
            if a in ("-", "--", "-s") or a.startswith("--") or a.startswith("-") or a.startswith("+"):
                k += 1
                continue
            if a in ("<", "<<", "<<-", "0<"):
                return STDIN
            return None         # a script file
        return STDIN
    return None


STDIN = "\x00stdin"


def check_find(args, cwd, cd_client):
    """`find <paths> ... -delete | -exec cmd ... | -ok ... | -fprint f`: a write when a start path is in client/
    (or relative after `cd client`). -exec/-ok of a read-only command (shasum, ls, ...) is still a read."""
    starts = []
    for a in args:
        if a.startswith("-") or a in ("(", "!", ")"):
            break
        starts.append(a)
    if not starts:
        starts = ["."]
    in_c = [a for a in starts if in_client(a, cwd) or (cd_client and not a.startswith("/") and "$" not in a)]
    for k, a in enumerate(args):
        if a in ("-fprint", "-fprint0", "-fprintf", "-fls") and k + 1 < len(args) and in_client(args[k + 1], cwd):
            block(f"`find {a}` writes {args[k + 1]}, inside client/.")
    if not in_c:
        return
    for k, a in enumerate(args):
        if a == "-delete":
            block(f"`find {in_c[0]} ... -delete` would delete files inside client/.")
        if a in ("-exec", "-execdir", "-ok", "-okdir"):
            run = args[k + 1] if k + 1 < len(args) else ""
            if os.path.basename(run) not in READ_ONLY or os.path.basename(run) in ("find",):
                block(f"`find {in_c[0]} ... {a} {run}` runs a command on files inside client/.")


def check_bash(cmd, cwd, depth=0):
    text = cmd
    # 1. symlinking the repo's runtime folders anywhere (ln -s / os.symlink / symlink_to) with one of them named
    links = re.search(r"(^|[\s;&|(])ln\s+(-[a-zA-Z]*s|--symbolic)", text) or re.search(r"os\.symlink|symlink_to\(", text)
    if links:
        named = [d for d in LINKED if re.search(r"(^|[\s/\"=$({])" + d + r"([\s/\"};)]|$)", text)]
        if named:
            block(f"this command creates symlinks and names the repo folder(s) {named}; linking client/, env/, "
                  "data/ or server/ into another folder lets a build in that folder write the real ones.")
    # 2. writes into client/
    cd_client = False
    for seg in segments(text):
        verb, args = verb_and_args(seg)
        base = os.path.basename(verb)
        if verb == "cd":
            cd_client = bool(args) and in_client(args[0], cwd)
            continue
        if verb in TRUSTED or verb.endswith("/harness/hs") or base == "git":
            continue
        inner = inner_shell_code(base, args)
        if inner == STDIN:
            # `echo 'cp x client/Wow.exe' | sh`, `sh <<EOF ... EOF`, `cat f | bash`: the code arrives on stdin, so
            # the hook judges the whole command: blocked when it names client/
            if re.search(r"(^|[\s/\"'=])client(/|\s|$)", text):
                block(f"`{base}` reads its code from stdin (a pipe or heredoc) in a command that names client/.")
            continue
        if inner is not None:
            # a wrapped shell (`bash -c "cp x client/Wow.exe"`, `eval ...`): the inner code gets the same rules;
            # code the hook can't parse to the end (too deep) is blocked only when it names client/
            if depth < 4:
                check_bash(inner, os.path.join(cwd, "client") if cd_client else cwd, depth + 1)
            elif re.search(r"(^|[\s/\"'=])client(/|\s|$)", inner):
                block("deeply nested shell code that names client/.")
            continue
        if base == "find":
            check_find(args, cwd, cd_client)
            continue
        # redirections inside this segment: > or >> followed by a target
        for j, t in enumerate(seg):
            if t in (">", ">>", ">|", "&>", "&>>") and j + 1 < len(seg) and in_client(seg[j + 1], cwd):
                block(f"output redirection writes {seg[j + 1]}, inside client/.")
        paths = [a for a in args if not a.startswith("-")]
        clientish = [a for a in paths if in_client(a, cwd) or (cd_client and not a.startswith("/") and "$" not in a)]
        if base in READ_ONLY:
            continue
        if base in ("tee",) and clientish:
            block(f"tee writes {clientish[0]}, inside client/.")
        if base in WRITE_ALL_ARGS and clientish:
            block(f"`{base}` would change {clientish[0]}, inside client/.")
        if base in WRITE_LAST_ARG:
            tgt = None
            if "-t" in args and args.index("-t") + 1 < len(args):
                tgt = args[args.index("-t") + 1]
            elif paths:
                tgt = paths[-1]
            if tgt and (in_client(tgt, cwd) or (cd_client and not tgt.startswith("/"))):
                block(f"`{base}` would write {tgt}, inside client/.")
        if base in INPLACE and any(a.startswith("-i") or a.startswith("--in-place") or a == "-pi" for a in args) and clientish:
            block(f"`{base} -i` would edit {clientish[0]}, inside client/.")
        if base == "dd" and any(a.startswith("of=") and in_client(a[3:], cwd) for a in args):
            block("dd of= writes inside client/.")
        if base in ("unzip", "tar", "bsdtar", "7z", "ditto") and clientish:
            block(f"`{base}` would extract into {clientish[0]}, inside client/.")
    # 3. interpreters with inline code that both names client/ and writes
    # only a write call whose own argument is a client path counts (a comment or a read that mentions client/ doesn't)
    if re.search(r"(^|[\s;&|(])(python3?|perl|ruby|node|osascript|swift)\b", text):
        CP = r"""[\"\x27](?:[^\"\x27]*/)?client/[^\"\x27]*[\"\x27]"""
        calls = [
            r"\bopen\(\s*" + CP + r"\s*,\s*[\"\x27][^\"\x27]*[wax+]",                       # open("client/x", "w")
            r"\b(?:os\.\w+|shutil\.\w+|copyfile|copy2?|move|rmtree|chflags|lchflags)\([^)]*" + CP,  # os.replace(.., "client/x")
            CP + r"\s*\)\s*\.(?:write_text|write_bytes|unlink|rename|replace|mkdir|touch|chmod|symlink_to|rmdir)\(",
        ]
        for pat in calls:
            m = re.search(pat, text)
            if m:
                tok = re.search(CP, m.group(0)).group(0).strip("\"\x27")
                if in_client(tok, cwd) or tok.startswith("client/"):
                    block(f"inline code writes {tok}; client/ is never written by hand.")


try:
    data = json.loads(os.environ.get("HS_GUARD_INPUT") or "")
except ValueError:
    sys.exit(0)
tool = data.get("tool_name", "")
inp = data.get("tool_input") or {}
cwd = data.get("cwd") or os.getcwd()
if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
    check_file_tool(tool, inp, cwd)
elif tool == "Bash":
    check_bash(inp.get("command") or "", cwd)
sys.exit(0)
PY

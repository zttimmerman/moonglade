/* Moonglade release builds: minimal readline stand-in (see include/readline/readline.h).
 * readline() prints the prompt and returns one line of stdin (without its newline); while it waits it calls rl_event_hook every 100 ms
 * and returns NULL as soon as the hook sets rl_done (how worldserver stops its console thread on shutdown).
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "readline/readline.h"
#include "readline/history.h"
#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

rl_completion_func_t *rl_attempted_completion_function = 0;
int rl_attempted_completion_over = 0;
char *rl_completer_word_break_characters = 0;
rl_hook_func_t *rl_event_hook = 0;
int rl_done = 0;

/* Input is read with read(2) into this buffer, never through stdio: stdio could buffer a second line that arrived
 * in the same read as the first, and poll() would then see an empty fd and leave that line waiting (release-first-
 * launch task 5.5). A complete line already in the buffer is returned without polling. */
static char *buf = NULL;
static size_t buf_len = 0, buf_cap = 0;
static int at_eof = 0;

static char *take_line(size_t n, int has_newline)
{
    char *line = malloc(n + 1);
    if (!line)
        return NULL;
    memcpy(line, buf, n);
    line[n] = '\0';
    size_t used = n + (has_newline ? 1 : 0);
    memmove(buf, buf + used, buf_len - used);
    buf_len -= used;
    return line;
}

char *readline(const char *prompt)
{
    if (prompt) {
        fputs(prompt, stdout);
        fflush(stdout);
    }
    rl_done = 0;
    for (;;) {
        char *nl = buf_len ? memchr(buf, '\n', buf_len) : NULL;
        if (nl)
            return take_line((size_t)(nl - buf), 1);
        if (at_eof) {
            if (buf_len)
                return take_line(buf_len, 0);
            return NULL;
        }
        struct pollfd p = {STDIN_FILENO, POLLIN, 0};
        int r = poll(&p, 1, 100);
        if (r > 0) {
            if (buf_cap - buf_len < 4096) {
                size_t cap = buf_cap ? buf_cap * 2 : 8192;
                char *nb = realloc(buf, cap);
                if (!nb)
                    return NULL;
                buf = nb;
                buf_cap = cap;
            }
            ssize_t n = read(STDIN_FILENO, buf + buf_len, buf_cap - buf_len);
            if (n > 0)
                buf_len += (size_t)n;
            else if (n == 0 || (errno != EINTR && errno != EAGAIN))
                at_eof = 1;
            continue;
        }
        if (rl_event_hook)
            rl_event_hook();
        if (rl_done)
            return NULL;
    }
}

char **rl_completion_matches(const char *text, rl_compentry_func_t *entry)
{
    (void)text;
    (void)entry;
    return NULL;
}

int rl_bind_key(int key, rl_command_func_t *fn)
{
    (void)key;
    (void)fn;
    return 0;
}

int rl_complete(int count, int key)
{
    (void)count;
    (void)key;
    return 0;
}

void add_history(const char *line)
{
    (void)line;
}

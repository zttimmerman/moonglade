/* Moonglade release builds: minimal readline stand-in (see include/readline/readline.h).
 * readline() prints the prompt and reads one line from stdin; while it waits it calls rl_event_hook every 100 ms
 * and returns NULL as soon as the hook sets rl_done (how worldserver stops its console thread on shutdown).
 * SPDX-License-Identifier: GPL-2.0-or-later */
#include "readline/readline.h"
#include "readline/history.h"
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

char *readline(const char *prompt)
{
    if (prompt) {
        fputs(prompt, stdout);
        fflush(stdout);
    }
    rl_done = 0;
    for (;;) {
        struct pollfd p = {STDIN_FILENO, POLLIN, 0};
        int r = poll(&p, 1, 100);
        if (r > 0)
            break;
        if (rl_event_hook)
            rl_event_hook();
        if (rl_done)
            return NULL;
    }
    char *line = NULL;
    size_t cap = 0;
    ssize_t n = getline(&line, &cap, stdin);
    if (n < 0) {
        free(line);
        return NULL;
    }
    if (n > 0 && line[n - 1] == '\n')
        line[n - 1] = '\0';
    return line;
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
